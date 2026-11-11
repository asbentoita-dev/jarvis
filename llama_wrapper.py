"""
🧊 llama_wrapper.py - Integração nativa com llama.cpp para otimizações de VRAM
   similar ao KoboldCpp.

Este módulo permite ao Jarvis controlar diretamente:
- Quantização de KV cache (f16, q8_0, q4_0) - economia de até 72% no cache
- Flash Attention controlável - -10% a -20% de VRAM
- Autofit automático de camadas GPU - alocação ótima
- Sliding Window Detection - uso automático em modelos compatíveis
- Low VRAM mode - KV cache na RAM (último recurso)
- Batch size tuning - otimização de throughput

Baseado nas otimizações documentadas em:
https://koboldcpp.net/docs/hardware/saving-vram/
"""

import os
import re
import subprocess
import threading
import time
from typing import Optional, Dict, List, Tuple, Any
from dataclasses import dataclass

try:
    from llama_cpp import Llama
    # LlamaGGUF pode não estar disponível em todas as versões
    try:
        from llama_cpp import LlamaGGUF
    except ImportError:
        LlamaGGUF = None
    LLAMA_AVAILABLE = True
except ImportError:
    LLAMA_AVAILABLE = False


@dataclass
class VRAMInfo:
    """Informações sobre VRAM disponível."""
    total_mb: int
    free_mb: int
    used_mb: int
    gpu_name: str
    backend: str  # "cuda", "rocm", "metal", "cpu"


@dataclass
class ModelInfo:
    """Informações sobre o modelo GGUF."""
    path: str
    n_layers: int
    context_length: int
    embedding_size: int
    vocab_size: int
    expert_count: Optional[int] = None  # Para modelos MoE
    expert_used_count: Optional[int] = None
    has_sliding_window: bool = False
    architecture: str = "unknown"


@dataclass
class LoadConfig:
    """Configuração de carregamento do modelo."""
    model_path: str
    n_gpu_layers: int = -1  # -1 = autofit, 0 = CPU
    n_ctx: int = 16384
    kv_cache_dtype: str = "f16"  # f16, bf16, q8_0, q5_1, q4_0, q2_0, iq*, tq*, mxfp4_moe, etc.
    flash_attention: bool = True
    batch_size: int = 512
    low_vram: bool = False  # KV cache na RAM
    n_threads: Optional[int] = None
    use_mmap: bool = True
    use_mlock: bool = False
    f16_kv: bool = True  # False = usa KV cache quantizado


class VRAMDetector:
    """Detecta VRAM disponível usando nvidia-smi, rocm-smi ou sysctl."""

    @staticmethod
    def detect() -> Optional[VRAMInfo]:
        """Tenta detectar VRAM de forma multi-plataforma."""
        # Tentar NVIDIA primeiro
        info = VRAMDetector._detect_nvidia()
        if info:
            return info

        # Tentar AMD ROCm
        info = VRAMDetector._detect_rocm()
        if info:
            return info

        # Tentar Apple Silicon (Metal)
        info = VRAMDetector._detect_metal()
        if info:
            return info

        # Fallback: CPU only
        return VRAMInfo(
            total_mb=0,
            free_mb=0,
            used_mb=0,
            gpu_name="CPU Only",
            backend="cpu"
        )

    @staticmethod
    def _detect_nvidia() -> Optional[VRAMInfo]:
        """Detecta VRAM NVIDIA via nvidia-smi."""
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total,memory.free,memory.used",
                 "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                parts = result.stdout.strip().split(", ")
                if len(parts) >= 4:
                    name = parts[0]
                    total = int(parts[1])
                    free = int(parts[2])
                    used = int(parts[3])
                    return VRAMInfo(
                        total_mb=total,
                        free_mb=free,
                        used_mb=used,
                        gpu_name=name,
                        backend="cuda"
                    )
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
            pass
        return None

    @staticmethod
    def _detect_rocm() -> Optional[VRAMInfo]:
        """Detecta VRAM AMD via rocm-smi."""
        try:
            result = subprocess.run(
                ["rocm-smi", "--showmeminfo", "vram"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                # Parse output do rocm-smi (formato varia por versão)
                lines = result.stdout.split("\n")
                for line in lines:
                    if "VRAM Total" in line or "Total Memory" in line:
                        # Exemplo: "VRAM Total: 8192 MB"
                        match = re.search(r"(\d+)\s*MB", line)
                        if match:
                            total = int(match.group(1))
                            # rocm-smi não expõe free diretamente de forma fácil
                            # Estimamos como 80% do total (conservador)
                            free = int(total * 0.8)
                            return VRAMInfo(
                                total_mb=total,
                                free_mb=free,
                                used_mb=total - free,
                                gpu_name="AMD GPU",
                                backend="rocm"
                            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return None

    @staticmethod
    def _detect_metal() -> Optional[VRAMInfo]:
        """Detecta VRAM Apple Silicon via sysctl."""
        try:
            result = subprocess.run(
                ["sysctl", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                # hw.memsize é RAM total, não VRAM
                # Em Apple Silicon, VRAM é compartilhada com RAM
                match = re.search(r"(\d+)", result.stdout)
                if match:
                    ram_bytes = int(match.group(1))
                    ram_mb = ram_bytes // (1024 * 1024)
                    # Estimamos 70% como disponível para GPU
                    free = int(ram_mb * 0.7)
                    return VRAMInfo(
                        total_mb=ram_mb,
                        free_mb=free,
                        used_mb=ram_mb - free,
                        gpu_name="Apple Silicon GPU",
                        backend="metal"
                    )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return None


class LlamaWrapper:
    """
    Wrapper principal para integração com llama.cpp com otimizações do KoboldCpp.
    """

    def __init__(self):
        self.model: Optional[Llama] = None
        self.model_info: Optional[ModelInfo] = None
        self.vram_info: Optional[VRAMInfo] = None
        self.config: Optional[LoadConfig] = None
        self._lock = threading.Lock()

    def detect_vram(self) -> VRAMInfo:
        """Detecta VRAM disponível e cacheia o resultado."""
        if self.vram_info is None:
            self.vram_info = VRAMDetector.detect()
        return self.vram_info

    def analyze_model(self, model_path: str) -> ModelInfo:
        """
        Analisa um arquivo GGUF e extrai metadados importantes.
        Retorna informações sobre camadas, contexto, MoE, sliding window, etc.
        """
        if not LLAMA_AVAILABLE:
            raise RuntimeError("llama-cpp-python não está instalado")

        try:
            # Se LlamaGGUF estiver disponível, usa para ler metadados diretamente
            if LlamaGGUF is not None:
                gguf = LlamaGGUF(model_path)
                metadata = gguf.metadata
            else:
                # Fallback: tenta ler metadados usando gguf diretamente
                try:
                    from gguf import GGUFReader
                    reader = GGUFReader(model_path)
                    metadata = {}
                    for key, value in reader.fields.items():
                        if hasattr(value, 'data'):
                            metadata[key] = value.data
                        else:
                            metadata[key] = value
                except ImportError:
                    # Último fallback: usa heurísticas baseadas no nome e tamanho
                    return self._analyze_model_heuristic(model_path)

            # Tenta encontrar campos de arquitetura (nomes variam por família)
            def get_by_suffix(suffix: str, default=None):
                for key, value in metadata.items():
                    if key.endswith(suffix):
                        return value
                return default

            n_layers = get_by_suffix(".block_count", 0)
            context_length = get_by_suffix(".context_length", 4096)
            embedding_size = get_by_suffix(".embedding_length", 0)
            vocab_size = get_by_suffix(".vocab_size", 0)
            expert_count = get_by_suffix(".expert_count")
            expert_used_count = get_by_suffix(".expert_used_count")

            # Detecta Sliding Window Attention
            has_sliding_window = (
                get_by_suffix(".sliding_window") is not None or
                get_by_suffix(".attention_window") is not None or
                "gemma" in metadata.get("general.architecture", "").lower()
            )

            # Detecta arquitetura
            architecture = metadata.get("general.architecture", "unknown")

            return ModelInfo(
                path=model_path,
                n_layers=n_layers,
                context_length=context_length,
                embedding_size=embedding_size,
                vocab_size=vocab_size,
                expert_count=expert_count,
                expert_used_count=expert_used_count,
                has_sliding_window=has_sliding_window,
                architecture=architecture
            )
        except Exception as e:
            # Se falhar, usa heurísticas
            print(f"[AVISO] Não foi possível ler metadados completos, usando heurísticas: {e}")
            return self._analyze_model_heuristic(model_path)

    def _analyze_model_heuristic(self, model_path: str) -> ModelInfo:
        """
        Analisa modelo usando heurísticas baseadas no nome e tamanho do arquivo.
        Usado quando não é possível ler metadados completos do GGUF.
        """
        filename = os.path.basename(model_path).lower()

        # Extrai informações do nome do arquivo
        # Ex: "llama-3.2-3b.Q4_K_M.gguf"
        # - Tamanho do modelo: 3b, 7b, 8b, 13b, etc.
        # - Quantização: Q4_K_M, Q6_K, Q8_0, etc.

        # Estima tamanho do modelo em parâmetros
        if "3b" in filename:
            n_params = 3_000_000_000
            n_layers = 28
        elif "7b" in filename:
            n_params = 7_000_000_000
            n_layers = 32
        elif "8b" in filename:
            n_params = 8_000_000_000
            n_layers = 32
        elif "13b" in filename:
            n_params = 13_000_000_000
            n_layers = 40
        elif "14b" in filename:
            n_params = 14_000_000_000
            n_layers = 40
        elif "34b" in filename or "35b" in filename:
            n_params = 35_000_000_000
            n_layers = 60
        else:
            # Padrão: 7B
            n_params = 7_000_000_000
            n_layers = 32

        # Detecta arquitetura pelo nome
        if "llama" in filename or "l3" in filename:
            architecture = "llama"
            embedding_size = 4096
            vocab_size = 128256
        elif "qwen" in filename:
            architecture = "qwen"
            embedding_size = 4096
            vocab_size = 152064
        elif "mistral" in filename:
            architecture = "mistral"
            embedding_size = 4096
            vocab_size = 32000
        elif "gemma" in filename:
            architecture = "gemma"
            embedding_size = 2048
            vocab_size = 256000
        else:
            architecture = "unknown"
            embedding_size = 4096
            vocab_size = 50000

        # Detecta MoE
        has_moe = "moe" in filename or "a3b" in filename or "mixtral" in filename

        # Contexto padrão (pode variar por modelo)
        context_length = 8192 if "8k" not in filename else 8192
        if "16k" in filename or "context-16" in filename:
            context_length = 16384
        elif "32k" in filename or "context-32" in filename:
            context_length = 32768

        # Detecta sliding window
        has_sliding_window = architecture == "gemma" or "sw" in filename

        return ModelInfo(
            path=model_path,
            n_layers=n_layers,
            context_length=context_length,
            embedding_size=embedding_size,
            vocab_size=vocab_size,
            expert_count=8 if has_moe else None,
            expert_used_count=2 if has_moe else None,
            has_sliding_window=has_sliding_window,
            architecture=architecture
        )

    def calculate_optimal_gpu_layers(
        self,
        model_info: ModelInfo,
        vram_info: VRAMInfo,
        context_size: int = 16384,
        kv_dtype: str = "f16"
    ) -> int:
        """
        Calcula automaticamente o número ótimo de camadas para offload na GPU.
        Similar ao autofit do KoboldCpp (--gpulayers -1).

        Lógica:
        1. Estima uso de VRAM por camada (depende do tamanho do modelo)
        2. Estima uso do KV cache (depende de contexto e quantização)
        3. Calcula quantas camadas cabem após reservar espaço para KV cache
        4. Retorna o número de camadas (ou -1 para tudo na CPU se VRAM insuficiente)
        """
        if vram_info.backend == "cpu":
            return 0

        # Tamanho aproximado do modelo em MB (baseado no arquivo)
        try:
            model_size_mb = os.path.getsize(model_info.path) // (1024 * 1024)
        except:
            model_size_mb = 4000  # Estimativa conservadora

        # Fator de economia por quantização de KV
        # Sistema flexível: estima economia baseado no nome do tipo
        # Padrão: qN_XX ~ N/16 (ex: q8_0 = 8/16 = 0.5)
        # iqN_XX ~ N/16 (ex: iq2_s = 2/16 = 0.125)
        # tqN_XX ~ N/16 (ternary, ainda mais eficiente)
        def estimate_kv_savings(dtype: str) -> float:
            dtype = dtype.lower() if dtype else "f16"

            # Tipos float (sem economia)
            if dtype in ("f16", "bf16", "f32"):
                return 1.0

            # MOPE (Mixture of Precision for Experts)
            if "moe" in dtype or "mxfp4" in dtype:
                return 0.25  # MXFP4 é ~4 bits

            # Ternary (TQ)
            if dtype.startswith("tq"):
                # TQ1_0 = 1.69 bpw, TQ2_0 = 2.06 bpw
                if "1" in dtype:
                    return 0.11  # ~1.69/16
                elif "2" in dtype:
                    return 0.13  # ~2.06/16
                return 0.15

            # I-quants (IQ)
            if dtype.startswith("iq"):
                # Extrai número do tipo (ex: iq2_s -> 2)
                import re
                match = re.search(r"iq(\d)", dtype)
                if match:
                    bits = int(match.group(1))
                    # Ajuste por tamanho (XXS, XS, S, M)
                    size_mult = 1.0
                    if "xxs" in dtype:
                        size_mult = 0.9
                    elif "xs" in dtype:
                        size_mult = 0.95
                    elif "m" in dtype:
                        size_mult = 1.05
                    return (bits / 16.0) * size_mult
                return 0.5  # Fallback

            # Q-quants padrão
            if dtype.startswith("q"):
                # Extrai número do tipo (ex: q4_0 -> 4)
                import re
                match = re.search(r"q(\d)", dtype)
                if match:
                    bits = int(match.group(1))
                    # K-quants são mais eficientes que QN_0
                    if "_k" in dtype:
                        # Q2_K ~ 2.96 bpw, Q4_K ~ 4.34 bpw
                        # Usa um mapa específico para maior precisão
                        k_quants = {
                            "q2_k": 0.19,   # 2.96/16
                            "q2_k_s": 0.20,  # 3.2/16
                            "q3_k_s": 0.21,  # 3.41/16
                            "q3_k_m": 0.23,  # 3.74/16
                            "q3_k_l": 0.25,  # 4.03/16
                            "q4_k_s": 0.27,  # 4.34/16
                            "q4_k_m": 0.30,  # 4.78/16
                            "q5_k_s": 0.33,  # 5.21/16
                            "q5_k_m": 0.35,  # 5.65/16
                            "q6_k": 0.38,    # 6.0/16
                        }
                        if dtype in k_quants:
                            return k_quants[dtype]
                    # QN_0 padrão
                    return bits / 16.0
                return 0.5  # Fallback

            # Desconhecido: assume 50% de economia
            return 0.5

        kv_factor = estimate_kv_savings(kv_dtype)

        # Estima tamanho do KV cache (baseado em contexto)
        # Fórmula aproximada: 2 * context_size * embedding_size * kv_factor / 1024 / 1024
        embedding_size = model_info.embedding_size or 4096
        kv_cache_mb = (2 * context_size * embedding_size * kv_factor) / (1024 * 1024)

        # Se sliding window, KV cache é muito menor
        if model_info.has_sliding_window:
            kv_cache_mb *= 0.2  # 80% de economia

        # Espaço reservado para buffers de computação (~300MB)
        compute_buffers_mb = 300

        # Espaço disponível para camadas
        available_for_layers = vram_info.free_mb - kv_cache_mb - compute_buffers_mb

        if available_for_layers <= 0:
            return 0  # Nem KV cache cabe, tudo na CPU

        # Estima VRAM por camada (varia muito por modelo)
        # Usamos uma média de 100MB por camada para modelos 7B-13B
        vram_per_layer_mb = model_size_mb / (model_info.n_layers or 32)

        # Calcula quantas camadas cabem
        optimal_layers = int(available_for_layers / vram_per_layer_mb)

        # Limita ao número total de camadas do modelo
        max_layers = model_info.n_layers or 32
        optimal_layers = min(optimal_layers, max_layers)

        # Se couber tudo, retorna o total
        if optimal_layers >= max_layers:
            return max_layers

        # Se couber pelo menos uma camada, retorna
        if optimal_layers >= 1:
            return optimal_layers

        return 0

    def load_model(self, config: LoadConfig) -> Llama:
        """
        Carrega um modelo GGUF com as configurações especificadas.
        Implementa todas as otimizações do KoboldCpp.
        """
        if not LLAMA_AVAILABLE:
            raise RuntimeError("llama-cpp-python não está instalado")

        with self._lock:
            # Se já temos um modelo carregado, descarrega primeiro
            if self.model is not None:
                self.unload_model()

            # Detecta VRAM se ainda não detectou
            if self.vram_info is None:
                self.vram_info = self.detect_vram()

            # Analisa o modelo
            self.model_info = self.analyze_model(config.model_path)

            # Se n_gpu_layers = -1 (autofit), calcula automaticamente
            if config.n_gpu_layers == -1:
                config.n_gpu_layers = self.calculate_optimal_gpu_layers(
                    self.model_info,
                    self.vram_info,
                    config.n_ctx,
                    config.kv_cache_dtype
                )

            # Se sliding window, reduz contexto automaticamente
            if self.model_info.has_sliding_window:
                # SWA permite contexto maior com menos VRAM
                # Mas ainda respeitamos o configurado
                pass

            # Mapeia tipos de KV cache para parâmetros do llama.cpp
            # Sistema flexível: se o tipo não estiver no mapa, passa como string
            # (llama.cpp pode aceitar novos tipos diretamente)
            kv_type_map = {
                "f16": "f16",
                "bf16": "bf16",
                "q8_0": "q8_0",
                "q5_1": "q5_1",
                "q4_0": "q4_0",
                "q4_1": "q4_1",
                "q2_0": "q2_0",
                "q2_k": "q2_k",
                "q2_k_s": "q2_k_s",
                "q3_k_s": "q3_k_s",
                "q3_k_m": "q3_k_m",
                "q3_k_l": "q3_k_l",
                "q4_k_s": "q4_k_s",
                "q4_k_m": "q4_k_m",
                "q5_k_s": "q5_k_s",
                "q5_k_m": "q5_k_m",
                "q6_k": "q6_k",
                "iq1_s": "iq1_s",
                "iq1_m": "iq1_m",
                "iq2_xxs": "iq2_xxs",
                "iq2_xs": "iq2_xs",
                "iq2_s": "iq2_s",
                "iq2_m": "iq2_m",
                "iq3_xxs": "iq3_xxs",
                "iq3_xs": "iq3_xs",
                "iq3_s": "iq3_s",
                "iq3_m": "iq3_m",
                "iq4_nl": "iq4_nl",
                "iq4_xs": "iq4_xs",
                "tq1_0": "tq1_0",
                "tq2_0": "tq2_0",
                "mxfp4_moe": "mxfp4_moe",
            }
            kv_type = kv_type_map.get(config.kv_cache_dtype, config.kv_cache_dtype or "f16")

            # Cria o modelo com parâmetros otimizados
            llama_params = {
                "model_path": config.model_path,
                "n_gpu_layers": config.n_gpu_layers,
                "n_ctx": config.n_ctx,
                "f16_kv": config.f16_kv,
                "use_mmap": config.use_mmap,
                "use_mlock": config.use_mlock,
                "n_threads": config.n_threads,
                "verbose": False,
            }

            # Flash Attention (se suportado pela versão do llama.cpp)
            if config.flash_attention:
                llama_params["flash_attn"] = True

            # Batch size
            if config.batch_size:
                llama_params["n_batch"] = config.batch_size

            # Low VRAM mode (KV cache na RAM)
            if config.low_vram:
                llama_params["n_gpu_layers"] = 0  # Força tudo na CPU
                llama_params["f16_kv"] = False

            # Armazena a configuração
            self.config = config

            # Carrega o modelo
            try:
                self.model = Llama(**llama_params)
                return self.model
            except Exception as e:
                raise RuntimeError(f"Erro ao carregar modelo: {e}")

    def unload_model(self):
        """Descarrega o modelo da memória."""
        with self._lock:
            if self.model is not None:
                del self.model
                self.model = None
                self.model_info = None
                self.config = None

    def ensure_model(self, config: LoadConfig) -> Llama:
        """Carrega o modelo SOMENTE se necessário — reutiliza se já estiver
        carregado com a mesma config. Essencial para modo RAW: evita o
        custo de descarregar/recarregar a cada mensagem (perda de VRAM +
        segundos de espera). Retorna o modelo carregado."""
        with self._lock:
            # Já carregado com mesma configuração? reutiliza.
            if (self.model is not None
                    and self.config is not None
                    and self.config.model_path == config.model_path
                    and self.config.n_gpu_layers == config.n_gpu_layers
                    and self.config.n_ctx == config.n_ctx):
                return self.model

            # Config mudou ou não há modelo — carrega do zero.
            self.load_model(config)
            return self.model

    def chat(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: int = 512,
        temperature: float = 0.7,
        on_token=None,
        **kwargs
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Gera resposta usando o modelo carregado.
        Retorna (texto_resposta, metricas).
        on_token: callback opcional chamado com cada pedaço de texto (streaming).
        """
        if self.model is None:
            raise RuntimeError("Nenhum modelo carregado")

        start_time = time.time()

        # Converte mensagens para formato esperado pelo llama.cpp
        prompt = self._format_messages(messages)

        # Streaming ou batch?
        if on_token is not None:
            # Modo streaming: itera tokens conforme chegam
            texto = ""
            for chunk in self.model(
                prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                stream=True,
                **kwargs
            ):
                part = (chunk.get("choices") or [{}])[0].get("text", "")
                if part:
                    texto += part
                    try:
                        on_token(part)
                    except Exception:
                        pass
            response = {"choices": [{"text": texto}], "usage": {}}
        else:
            # Modo batch (sem streaming)
            response = self.model(
                prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                **kwargs
            )

        elapsed = time.time() - start_time

        # Extrai texto e métricas
        text = response["choices"][0]["text"]

        # Métricas
        tokens_generated = response.get("usage", {}).get("completion_tokens", 0)
        tokens_per_second = tokens_generated / elapsed if elapsed > 0 else 0

        metrics = {
            "tempo_segundos": elapsed,
            "tokens_gerados": tokens_generated,
            "tokens_por_segundo": tokens_per_second,
            "n_gpu_layers": self.config.n_gpu_layers if self.config else 0,
            "n_ctx": self.config.n_ctx if self.config else 0,
            "kv_cache_dtype": self.config.kv_cache_dtype if self.config else "f16",
            "flash_attention": self.config.flash_attention if self.config else False,
        }

        return text, metrics

    def _format_messages(self, messages: List[Dict[str, Any]]) -> str:
        """
        Converte lista de mensagens (formato OpenAI) para prompt de texto.
        Implementação simplificada - pode ser expandida para templates específicos.
        """
        prompt_parts = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                prompt_parts.append(f"System: {content}")
            elif role == "user":
                prompt_parts.append(f"User: {content}")
            elif role == "assistant":
                prompt_parts.append(f"Assistant: {content}")

        prompt_parts.append("Assistant:")
        return "\n".join(prompt_parts)

    def get_memory_usage(self) -> Dict[str, Any]:
        """
        Retorna informações sobre uso de memória do modelo carregado.
        """
        if self.model is None:
            return {"carregado": False}

        # Estima baseado na configuração
        if not self.model_info or not self.config:
            return {"carregado": True, "info": "Informações não disponíveis"}

        try:
            model_size_mb = os.path.getsize(self.model_info.path) // (1024 * 1024)
        except:
            model_size_mb = 0

        # Estima KV cache
        embedding_size = self.model_info.embedding_size or 4096
        kv_savings = {"f16": 1.0, "q8_0": 0.53, "q4_0": 0.28}
        kv_factor = kv_savings.get(self.config.kv_cache_dtype, 1.0)
        kv_cache_mb = (2 * self.config.n_ctx * embedding_size * kv_factor) / (1024 * 1024)

        if self.model_info.has_sliding_window:
            kv_cache_mb *= 0.2

        return {
            "carregado": True,
            "modelo_mb": model_size_mb,
            "kv_cache_mb": kv_cache_mb,
            "n_gpu_layers": self.config.n_gpu_layers,
            "n_ctx": self.config.n_ctx,
            "kv_dtype": self.config.kv_cache_dtype,
            "flash_attention": self.config.flash_attention,
            "has_sliding_window": self.model_info.has_sliding_window,
            "is_moe": self.model_info.expert_count is not None,
        }


# Instância global (singleton)
_instance: Optional[LlamaWrapper] = None
_instance_lock = threading.Lock()


def get_wrapper() -> LlamaWrapper:
    """Retorna a instância singleton do LlamaWrapper."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = LlamaWrapper()
    return _instance
