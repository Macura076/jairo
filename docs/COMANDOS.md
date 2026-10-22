# Comandos

Todo comando começa com a palavra de ativação **"Jairo"** (também aceita "ei Jairo", "oi Jairo"). Dizer só "Jairo" faz ele responder "Estou aqui!". As variações de reconhecimento ficam em `JAIRO_WAKE_ALIASES`.

Os gatilhos são comparados sem acentos e sem diferenciar maiúsculas. Os exemplos abaixo mostram as formas principais, não todas as variações aceitas.

## Feedback

- **Bipe curto:** ação executada.
- **Bipe grave:** comando não reconhecido (o texto entendido aparece no log).
- **Fala:** respostas com informação e confirmações de ações mais longas. No modo silencioso, as confirmações viram bipes e só as respostas de informação continuam faladas.

## Captura de tela

| Comando | Efeito |
| --- | --- |
| "tirar print", "print", "captura de tela", "recortar tela" | Abre o recorte do Windows (Win+Shift+S); você seleciona a área |
| "print da tela inteira", "screenshot rápido" | Salva a tela inteira em Imagens\Capturas de Tela (Win+PrtSc) |

## Sistema

| Comando | Efeito |
| --- | --- |
| "bloquear o computador", "bloquear o pc", "travar a tela", "bloquear" | Bloqueia a sessão (`LockWorkStation`) |
| "desligar o computador", "sono eterno" | Agenda o desligamento em 60 segundos |
| "cancelar desligamento" | Cancela (`shutdown /a`) |
| "desligar jairo", "fechar sistema", "encerrar sistema" | Encerra o assistente |
| "silenciar", "modo espera", "descansar", "dormir" | Standby: só "Jairo" (ou "Jairo, ativar") o acorda |
| "recalibrar microfone" | Recalibra o limite de energia do microfone |

## Janelas

| Comando | Atalho simulado |
| --- | --- |
| "minimizar tudo", "mostrar área de trabalho" | Win+D |
| "trocar de janela", "próxima janela" | Alt+Tab |
| "maximizar janela" | Win+Seta para cima |
| "minimizar janela" | Win+Seta para baixo |
| "janela para a esquerda" / "direita" | Win+Seta lateral |
| "fechar janela" | Alt+F4 |

## Mídia e volume

| Comando | Efeito |
| --- | --- |
| "pausar", "play", "continuar", "tocar música" | Alterna tocar/pausar (tecla de mídia) |
| "próxima música", "pular música" | Próxima faixa |
| "música anterior", "voltar música" | Faixa anterior |
| "aumentar volume", "aumentar o volume em 20" | +10% (ou o valor dito) |
| "diminuir volume", "baixar o volume em 20" | -10% (ou o valor dito) |
| "volume 50", "volume para 50 por cento" | Define o volume |
| "qual o volume" | Fala o volume atual (requer `pycaw`) |
| "mutar", "desmutar" | Silencia / reativa o som |

Sem `pycaw`, o volume é ajustado por teclas (2% por toque) e o valor atual não pode ser lido.

## Contas

| Comando | Resposta |
| --- | --- |
| "quanto é 15 + 14", "15 mais 14" | "29" |
| "calcule 10 vezes 3", "quanto é 3 x 4" | "30", "12" |
| "quanto é 100 dividido por 8" | "12,5" |
| "quanto é 10 menos 25" | "menos 15" |
| "quanto é 20 por cento de 150" | "30" |
| "raiz quadrada de 144" | "12" |
| "quanto é 2 elevado a 10", "5 ao quadrado" | "1024", "25" |
| "quanto é vinte e cinco mais dois" | "27" |

Aceita números por extenso simples, vírgula decimal e separador de milhar (`1.500`). Divisão por zero responde que não é possível.

## Timers

Ficam em memória e não sobrevivem a um reinício do JAIRO.

| Comando | Efeito |
| --- | --- |
| "timer de 5 minutos", "temporizador de 30 segundos" | Cria o timer |
| "me lembra em meia hora de tomar água" | Timer com rótulo |
| "quanto falta" | Lista os timers ativos |
| "cancelar timers" | Cancela todos |

Ao terminar, tocam três bipes e o JAIRO fala "Tempo esgotado" com o rótulo.

## WhatsApp

| Comando | Efeito |
| --- | --- |
| "enviar mensagem para Maria dizendo estou chegando" | Envia e confirma pela bolha de saída |
| "ler mensagens", "mensagens não lidas", "tenho mensagem" | Lê o resumo das não lidas sem abrir as conversas |
| "verificar mensagens", "varrer whatsapp" | Roda o auto-responder agora |
| "buscar contato gustavo" | Lista os contatos cadastrados que casam com o nome |
| "diagnosticar whatsapp" | Grava `wpp_diagnostico.json` para ajustar seletores |

Se o nome falado casar com mais de um contato, o JAIRO diz quais e pede o nome completo.

## Aplicativos e web

| Comando | Efeito |
| --- | --- |
| "abre o vs code", "abrir discord", "abre spotify" | Abre o aplicativo |
| "abre projetos", "abre downloads", "abre configurações" | Abre a pasta ou tela |
| "preparar ambiente", "modo trabalho" | Abre VS Code e Discord |
| "pesquisar receita de bolo" | Busca no Google |
| "youtube lofi" | Busca no YouTube |

Os aplicativos disponíveis ficam em `AppLauncher._APPS` no `jairo.py`; ajuste os caminhos para a sua máquina.

## Informações e modos

| Comando | Efeito |
| --- | --- |
| "que horas são" | Fala a hora |
| "que dia é hoje" | Fala a data |
| "repetir" | Repete a última resposta |
| "ajuda" | Fala um resumo dos comandos |
| "modo silencioso" / "modo falante" | Alterna bipes e fala nas confirmações |
