// 🔌 Plugin: puppeteer_avancado.js
// Usando Puppeteer (Node.js) para captura de páginas web

const puppeteer = require('puppeteer');
const path = require('path');
const fs = require('fs');

let browser = null;
let page = null;

async function init() {
    if (!browser) {
        browser = await puppeteer.launch({
            headless: true,
            args: ['--no-sandbox', '--disable-setuid-sandbox'],
            executablePath: 'C:/Users/Casa/.cache/puppeteer/chrome/win64-149.0.7827.22/chrome-win64/chrome.exe'
        });
        page = await browser.newPage();
        await page.setViewport({ width: 1280, height: 900 });
    }
    return page;
}

async function abrir_pagina(url) {
    const p = await init();
    await p.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
    const title = await p.title();
    const texto = await p.evaluate(() => document.body.innerText);
    return `[Página: ${title}]\n${url}\n\n${texto.substring(0, 3000)}`;
}

async function capturar_tela(nome = 'puppeteer.png') {
    const p = await init();
    const caminho = path.join(process.env.WORKSPACE || 'C:/IA/Jarvis/Jarvis Claude/workspace', nome);
    await p.screenshot({ path: caminho, fullPage: true });
    return `IMAGEM_GERADA: ${nome} (${caminho})`;
}

async function extrair_texto() {
    const p = await init();
    const texto = await p.evaluate(() => document.body.innerText);
    return texto.substring(0, 5000);
}

async function extrair_html() {
    const p = await init();
    const html = await p.content();
    return html.substring(0, 10000);
}

async function fechar() {
    if (browser) {
        await browser.close();
        browser = null;
        page = null;
    }
    return 'Navegador Puppeteer fechado.';
}

// Exportar para o sistema de plugins
module.exports.TOOLS = [
    {
        type: 'function',
        function: {
            name: 'abrir_pagina_puppeteer',
            description: 'Abre um site com Puppeteer e retorna o texto.',
            parameters: {
                type: 'object',
                properties: {
                    url: { type: 'string', description: 'URL do site' }
                },
                required: ['url']
            }
        }
    },
    {
        type: 'function',
        function: {
            name: 'capturar_tela_puppeteer',
            description: 'Tira screenshot da página atual.',
            parameters: {
                type: 'object',
                properties: {
                    nome: { type: 'string', description: 'Nome do arquivo (padrão: puppeteer.png)' }
                }
            }
        }
    },
    {
        type: 'function',
        function: {
            name: 'extrair_texto_puppeteer',
            description: 'Extrai o texto visível da página.',
            parameters: { type: 'object', properties: {} }
        }
    },
    {
        type: 'function',
        function: {
            name: 'extrair_html_puppeteer',
            description: 'Extrai o HTML da página.',
            parameters: { type: 'object', properties: {} }
        }
    },
    {
        type: 'function',
        function: {
            name: 'fechar_puppeteer',
            description: 'Fecha o navegador Puppeteer.',
            parameters: { type: 'object', properties: {} }
        }
    }
];

module.exports.FERRAMENTAS = {
    'abrir_pagina_puppeteer': abrir_pagina,
    'capturar_tela_puppeteer': capturar_tela,
    'extrair_texto_puppeteer': extrair_texto,
    'extrair_html_puppeteer': extrair_html,
    'fechar_puppeteer': fechar
};