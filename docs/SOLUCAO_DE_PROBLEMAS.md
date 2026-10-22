# Solução de problemas

O log completo fica em `jairo.log` (na pasta onde você executou o programa). Use `JAIRO_DEBUG=true` para ver a janela do Brave.

## Não ouço a voz do JAIRO

1. Rode `python jairo.py --testar-voz`. Ele lista as vozes instaladas e testa os dois motores, um após o outro.
2. Se você ouviu só o segundo teste, defina `JAIRO_TTS=powershell` no `.env`. Se ouviu só o primeiro, defina `JAIRO_TTS=pyttsx3`.
3. Se não ouviu nenhum: confira o dispositivo de saída padrão do Windows, o mixer de volume (o volume do Python/terminal) e se há voz em português instalada (Configurações > Hora e idioma > Fala).

Por padrão (`auto`), o JAIRO detecta uma fala silenciosa do `pyttsx3` e troca sozinho para o PowerShell.

## Preciso falar alto ou perto do microfone

1. Rode `python jairo.py --testar-mic` e siga as instruções (3 s de silêncio e 4 s de fala).
2. Coloque o valor sugerido em `JAIRO_ENERGY` no `.env`.
3. Aumente o volume de entrada em Configurações > Sistema > Som > Entrada e teste desativar "melhorias de áudio" do microfone.
4. Aumente `JAIRO_GANHO_MAX` (padrão 4.0) para amplificar frases baixas antes do reconhecimento.
5. Diga "Jairo, recalibrar microfone" quando o ambiente mudar.

Se ele parece cortar o fim das frases, aumente `JAIRO_PAUSE`; se parece demorar para executar, diminua (padrão 0.8).

## Ele não entende a palavra "Jairo"

O reconhecedor às vezes transcreve o nome de outro jeito. Veja no log a linha `Ignorado (sem palavra de ativacao)` para descobrir como saiu e adicione a variação em `JAIRO_WAKE_ALIASES`.

## "Você não tem mensagens não lidas", mas eu tenho

O WhatsApp Web muda o HTML com frequência.

1. Diga "Jairo, diagnosticar whatsapp".
2. Abra `wpp_diagnostico.json` (em `JAIRO_DIR`). Ele traz a contagem de elementos por seletor, os rótulos `aria-label` da lista e o HTML de amostra de algumas conversas.
3. Os campos `total_itens` e `coleta_js` mostram se a lista foi encontrada. Se `total_itens` for 0, o seletor da lista mudou.

O WhatsApp só renderiza as conversas visíveis e não inclui as arquivadas. Se a mensagem estiver numa conversa arquivada ou rolada para baixo, o JAIRO não a vê.

> O arquivo contém nomes e prévias de mensagens reais. Revise antes de compartilhar.

## A mensagem foi para o contato errado

O JAIRO envia pelo **número** do `contatos.txt`. Se há um contato antigo com o mesmo nome, ele será usado.

1. Diga "Jairo, buscar contato NOME" para listar os cadastrados com os 4 últimos dígitos.
2. Desative o antigo com `!` no início da linha, ou corrija o número.
3. Se quiser que uma palavra curta aponte para um contato, use apelido: `Gustavo Novo:5511...:gustavo`.

Quando mais de um contato casa com o nome falado, ele pede o nome completo em vez de escolher um.

## `SessionNotCreatedException` ao abrir o Brave

A versão do `chromedriver` não corresponde à do Chromium do Brave.

- Atualize o Selenium (`pip install -U selenium`); as versões recentes baixam o driver compatível.
- Confirme que `BRAVE_PATH` aponta para o `brave.exe` correto.
- Se persistir, baixe manualmente o `chromedriver` da mesma versão principal do Brave (veja em `brave://version`) e deixe-o no `PATH`.

## Sessão do WhatsApp expirada

O log mostra "Sessao expirada". Apague `autenticado.txt` em `JAIRO_DIR` e reinicie; o Brave abre visível com o QR Code (3 minutos para escanear). Se o problema continuar, apague também a pasta `perfil_jairo`.

## O PyAudio não instala

Use uma versão com wheel pré-compilado para o seu Python: `pip install PyAudio --only-binary :all:`. Em Python 3.13 ou superior, instale também `audioop-lts` (já listado no `requirements.txt`).

## O volume não muda exatamente para o valor pedido

Instale `pycaw` (`pip install pycaw comtypes`). Sem ele, o JAIRO usa as teclas de volume (2% por toque) e não consegue ler o volume atual.

## O print de tela inteira ou o "trocar de janela" não respondem

Esses comandos simulam atalhos do Windows (Win+PrtSc e Alt+Tab). Alguns ambientes bloqueiam atalhos gerados por software. Abra uma issue com a versão do Windows.
