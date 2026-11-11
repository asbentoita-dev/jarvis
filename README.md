# Jarvis Claude

Framework de agente autônomo com suporte a múltiplos backends de IA (Ollama, LM Studio, KoboldCpp, Groq, 9router e outros).

## 🧠 O que é

Jarvis Claude é um agente de desktop que combina:
- **Loop agêntico** com classificação automática de tarefas (código, visão, raciocínio, geral)
- **Múltiplos backends de LLM**: locais (Ollama, LM Studio, KoboldCpp) e de nuvem (Groq, 9router, Claude, etc.)
- **Ferramentas**: navegação web, PDF, captura de tela, tradução, execução de código, geração de imagens, e muito mais
- **Memória vetorial** (ChromaDB) com backup automático
- **Interface web** (Flask) com chat em tempo real e painel de configuração

## 🚀 Instalação

### Requisitos
- Python 3.10+
- Ollama, LM Studio ou KoboldCpp instalado (para modelos locais)
- FFmpeg (para processamento de vídeo)
- Stable Diffusion API (opcional, para geração de imagens)

### Configuração
1. Copie `.env.example` para `.env` e preencha as chaves de API
2. Instale as dependências:
   ```bash
   pip install -r requirements.txt
   npm install
   ```
3. Inicie o Jarvis:
   ```bash
   python main.py
   ```

## 📦 Backends suportados

| Backend | Tipo | Aceleração | Comentário |
|---------|------|------------|------------|
| Ollama | Local | NVIDIA CUDA / CPU | Padrão; não suporta AMD no Windows |
| LM Studio | Local | CPU / CUDA | API compatível com OpenAI |
| KoboldCpp | Local | **Vulkan (AMD)** | Use `koboldcpp-nocuda.exe` para GPUs AMD |
| Groq | Nuvem | — | Cérebro veloz, plano free |
| 9router | Nuvem | — | Roteador próprio de múltiplas IAs |
| Claude | Nuvem | — | Para tarefas de alta qualidade |

## 🔌 Plugins

Os plugins em `plugins/` adicionar ferramentas ao agente:

| Plugin | Ferramentas |
|--------|-------------|
| `analise_codigo.py` | Identificar/arquivo, analisar código |
| `captura_tela.py` | Tirar print, gravar tela |
| `freebuff.py` | Delegar código |
| `missao.py` | Executar missão (planejar → executar → revisar) |
| `multiagente.py` | Time de especialistas (planejador, técnico, interface, analista, revisor) |
| `n8n.py` | Chamada n8n |
| `navegador_v2.py` | Navegação web completa (Playwright) |
| `pdf_editor.py` | Juntar, dividir, extrair páginas de PDF |
| `selenium.py` | Navegação web (Selenium) |
| `tradutor.py` | Tradução de texto e tela |

## ⚙️ Configuração

Veja `config.py` e `.env.example` para todas as opções. Principais:

- `SERVIDOR_LOCAL`: `ollama`, `lmstudio` ou `koboldcpp`
- `JARVIS_MODELO`: modelo padrão do Jarvis
- `AUTO_MODELO_ATIVO`: troca automaticamente de modelo conforme o pedido
- `LINHA_PRODUCAO_ATIVO`: classifica a tarefa e roteia pro fluxo certo
- `TIMEOUT_RESPOSTA`: timeout para chamadas de nuvem (segundos)
- `TIMEOUT_RESPOSTA_LOCAL`: timeout para chamadas locais (segundos)

## 📁 Estrutura

```
jarvis/
├── agent.py           # Loop agêntico principal
├── app.py             # Interface web (Flask)
├── config.py          # Configuração central
├── llm.py             # Camada de LLM (multi-backend)
├── web.py             # Rotas da interface web
├── main.py            # Ponto de entrada
├── plugins/           # Plugins de ferramentas
├── tools.py           | Ferramentas base
├── memory.py          # Memória vetorial (ChromaDB)
├── model_params.py    # Parâmetros de modelos
├── moe_tuner.py       # Auto-ajuste de modelos MoE
├── cerebros.py        # Gerenciamento de cérebros de nuvem
├── equipes.py         # Times de especialistas (multiagente)
├── mcp_config.py      # Configuração MCP
├── .env.example       # Template de configuração
└── .gitignore         # Arquivos ignorados pelo Git
```

## 🛠️ Desenvolvedor

- Nome: Bento
- Email: asbento.ita@gmail.com
- GitHub: [asbentoita-dev](https://github.com/asbentoita-dev)

## 📝 Licença

Este projeto é privado. Veja o arquivo `LICENSE` para detalhes.