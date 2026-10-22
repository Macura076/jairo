# Histórico de mudanças

## Revisão 5

- Microfone: limite mínimo de energia reduzido de 300 para 120 (`JAIRO_ENERGY_MIN`), ganho automático para voz baixa (`JAIRO_GANHO_MAX`), comando "recalibrar microfone" e diagnóstico `--testar-mic`.
- `JAIRO_PAUSE` com padrão 0.8 s.
- Calculadora por voz ("quanto é 15 + 14" responde "29").
- Agenda: apelidos, contatos inativos (`!`), nome ambíguo pede o nome completo, busca sem acentos e por termos, comando "buscar contato".

## Revisão 4 (latência e usabilidade)

- Ações instantâneas (print, bloqueio, janelas, mídia, volume) executam antes de qualquer fala; feedback por bipes.
- Microfone aberto continuamente; início do WhatsApp em paralelo com a calibração.
- Envio no WhatsApp sem esperas fixas: destinatário confirmado por polling do cabeçalho.
- Novos comandos: timers, janelas, print de tela inteira, data, repetir, ajuda, modo silencioso, atalhos para projetos/downloads/configurações.

## Revisão 3

- Leitura de não lidas reescrita: coleta via JavaScript tolerante a mudanças de DOM, com fallback para Selenium, e comando "diagnosticar whatsapp".
- Captura de tela (Win+Shift+S) e bloqueio do PC (`LockWorkStation`).

## Revisão 2

- Palavra de ativação obrigatória.
- Controle de mídia e volume.
- Leitura por voz das conversas não lidas.
- "Verificar mensagens" passa a disparar a varredura do auto-responder pela fila do worker.
- "Cancelar desligamento".
- TTS com motor recriado a cada fala, detecção de fala silenciosa, fallback para PowerShell e `--testar-voz`.
- Anti-eco: o microfone descarta o áudio captado durante a fala do JAIRO.

## v2.0

- WebDriver isolado em uma worker única; agenda com validação de números; timeout global de seletores; confirmação de envio por bolhas; auto-responder com fingerprint SHA-256; backoff com jitter; logging estruturado com mascaramento; `subprocess` sem `shell=True`; configuração via `.env`.
