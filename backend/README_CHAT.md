# Chat do Fluir - guia rapido

O chat e uma parte separada do site principal. O Flask (servidor das paginas do
Fluir) confirma quem entrou e abre o acesso ao FastAPI (servidor que cuida do
chat). O chat usa WebSocket (conexao que permite receber mensagens sem atualizar
a pagina) para entregar mensagens em tempo real.
O Service Worker e um recurso nativo do navegador, nao uma dependencia Python.
O arquivo `static/service-worker.js` guarda arquivos da interface e a pagina de
aviso offline. O `static/js/app.js` salva a ultima previsao climatica localmente.

Para entender o projeto inteiro, incluindo explicacoes simples e perguntas para
a apresentacao, consulte `../DOCUMENTACAO_SITE_SIPF.txt`.

## Como iniciar

Instale as dependencias na pasta `backend`:

```powershell
python -m pip install -r requirements.txt
```

O Flask cria e reutiliza um segredo forte em `instance/fluir.secret` para as
sessões locais e o repassa ao chat que ele inicia automaticamente. Se iniciar
Flask e FastAPI manualmente em terminais
separados, configure o mesmo `FLUIR_SECRET_KEY` nos dois antes de iniciar os
servidores. Gere um valor com `python -c "import secrets; print(secrets.token_hex(32))"`.

Abra um terminal para o Flask:

```powershell
python -m flask --app app run --host 127.0.0.1 --port 5000
```

Opcionalmente, o chat pode ser iniciado manualmente em outro terminal:

```powershell
python -m uvicorn chat_fastapi:app --reload --host 127.0.0.1 --port 8001
```

Acesse `http://127.0.0.1:5000/login` e entre pelo site. Na primeira vez que o
menu Chat for aberto, o Flask inicia o serviço FastAPI local na porta 8001 se
ele ainda não estiver rodando. Se o início falhar, o motivo fica registrado em
`instance/chat-server.log`. Também é possível iniciar o serviço manualmente
pelo segundo comando acima; nesse caso o Flask detecta e reutiliza o servidor
que já estiver ativo.

## Como as conversas sao separadas

- Pessoas do mesmo porto e setor podem usar o chat do setor, disponível para
  todos os funcionários aprovados daquele setor e porto.
- A busca encontra funcionarios do mesmo porto pelo nome completo e mostra o
  setor ou a funcao abaixo do nome.
- Conversas privadas sao criadas entre duas pessoas autorizadas do mesmo porto.
- Quando existem dois ou mais representantes aprovados no mesmo setor e porto,
  eles compartilham o Chat dos Representantes do Setor.
- Quando existem dois ou mais representantes aprovados no Fluir, eles também
  compartilham o Chat dos Representantes, comum a todos os setores e portos.
  Funcionários sem permissão de representante não acessam esses grupos.
- Representantes podem personalizar o nome e a foto dos grupos. O nome do grupo
  geral dos representantes é compartilhado entre todos os portos.
- A tela mostra o nome completo e o setor ou a funcao de representante.
- As mensagens ficam salvas em `instance/chat.db`.

## Redis e opcional

O chat tenta usar Redis para distribuir mensagens entre varios processos. Se o
Redis nao estiver instalado ou ligado, o chat usa conexoes guardadas na memoria
do proprio processo. Isso basta para a demonstracao local, mas processos
separados nao compartilham mensagens sem Redis.

Para ligar Redis com Docker, se o Docker estiver instalado:

```powershell
docker run --name fluir-redis -p 6379:6379 redis:7
```
