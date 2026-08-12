# YouTube Live Loop — Especificação para Implementação

Sistema em instância única que gerencia múltiplos canais do YouTube, baixa os X vídeos mais recentes de cada um, e transmite cada canal como uma live em loop, via uma interface web de configuração.

---

## 1. Stack

- **Backend**: Python 3.11+, FastAPI
- **Frontend**: HTMX + Jinja2 templates (sem build step de JS, tudo server-rendered)
- **Banco**: SQLite (arquivo único em `/data/app.db`)
- **Scheduler**: APScheduler (background jobs no mesmo processo)
- **Download de vídeo**: `yt-dlp`
- **Streaming**: `ffmpeg` (subprocessos gerenciados pelo próprio app)
- **Metadados de canal**: YouTube Data API v3
- **Deploy**: um único container Docker, via Coolify (docker-compose com 1 serviço)

Tudo roda em **um único processo/container**. Sem filas externas, sem Redis, sem múltiplos serviços.

---

## 2. Autenticação

- Usuário único, sem cadastro, sem múltiplos perfis.
- Credenciais vêm de variáveis de ambiente: `APP_USERNAME` e `APP_PASSWORD`.
- Implementar como middleware de sessão simples (cookie de sessão assinado, ex: `itsdangerous` ou `starlette.middleware.sessions.SessionMiddleware`).
- Tela de login única (`/login`) protegendo todas as rotas exceto `/login` e assets estáticos.
- Não usar OAuth, não usar banco de usuários — comparação direta com as envs (usar `secrets.compare_digest` para evitar timing attack).

---

## 3. Variáveis de ambiente

```
APP_USERNAME=            # usuário de login da interface web
APP_PASSWORD=             # senha de login da interface web
SESSION_SECRET=           # chave aleatória para assinar cookie de sessão
YOUTUBE_API_KEY=          # chave da YouTube Data API v3 (só leitura de metadados)
DATA_DIR=/data            # diretório persistente (montado como volume no Coolify)
DEFAULT_CHECK_INTERVAL_HOURS=6   # intervalo padrão de checagem de vídeos novos
```

Stream key de cada canal (RTMP) **não é env global** — é configurado por canal, na UI, e guardado no banco (ver schema abaixo).

---

## 4. Estrutura de arquivos do projeto

```
app/
  main.py                 # bootstrap FastAPI, monta rotas, inicia scheduler
  auth.py                 # login, middleware de sessão, checagem de credenciais
  db.py                   # conexão SQLite + criação de schema
  models.py                # dataclasses / Pydantic models
  routes/
    ui.py                  # rotas HTML (dashboard, form de canal)
    api.py                  # rotas de ação (start/stop/restart canal, refresh manual)
  services/
    fetcher.py              # YouTube Data API: lista últimos X vídeos do canal
    downloader.py           # yt-dlp: baixa vídeos novos
    playlist.py             # gera playlist.txt no formato concat demuxer
    stream_manager.py       # gerencia subprocessos ffmpeg (start/stop/monitor)
    scheduler.py             # jobs do APScheduler (checagem periódica por canal)
  templates/
    login.html
    dashboard.html
    channel_form.html
  static/
    style.css
Dockerfile
docker-compose.yml
requirements.txt
```

---

## 5. Modelo de dados (SQLite)

```sql
CREATE TABLE channels (
    id TEXT PRIMARY KEY,              -- slug interno, ex: "canal-a"
    youtube_channel_id TEXT NOT NULL, -- UCxxxxxxxx
    display_name TEXT NOT NULL,
    stream_key TEXT NOT NULL,         -- RTMP stream key (destino do YouTube Live)
    video_count INTEGER NOT NULL DEFAULT 20,   -- X vídeos
    check_interval_hours INTEGER NOT NULL DEFAULT 6,
    is_active BOOLEAN NOT NULL DEFAULT 1,       -- se deve estar transmitindo
    status TEXT NOT NULL DEFAULT 'stopped',     -- running | stopped | error
    last_checked_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE channel_videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL REFERENCES channels(id),
    youtube_video_id TEXT NOT NULL,
    file_path TEXT,                    -- caminho local após download
    downloaded_at TIMESTAMP,
    position INTEGER,                  -- ordem na playlist
    UNIQUE(channel_id, youtube_video_id)
);
```

---

## 6. Rotas

### UI (HTML, protegidas por login)
- `GET /login` / `POST /login`
- `GET /` — dashboard: lista de canais, status, botões liga/desliga/editar/remover
- `GET /channels/new` / `POST /channels/new` — form de criação
- `GET /channels/{id}/edit` / `POST /channels/{id}/edit`
- `POST /channels/{id}/delete`

### Ações (chamadas via HTMX, retornam fragmento HTML)
- `POST /channels/{id}/start` — ativa canal (is_active=1), dispara fetch+download+start do ffmpeg
- `POST /channels/{id}/stop` — mata o processo ffmpeg, is_active=0
- `POST /channels/{id}/refresh` — força checagem de vídeos novos agora (fora do schedule)

---

## 7. Lógica de negócio

### 7.1 Fetcher (`fetcher.py`)
- Usa YouTube Data API v3: primeiro resolve a "uploads playlist" do canal (`channels.list part=contentDetails`), depois `playlistItems.list` com `maxResults` = `video_count`, ordenado por mais recente.
- Retorna lista de `video_id`s.

### 7.2 Downloader (`downloader.py`)
- Para cada `video_id` que ainda não existe em `channel_videos`:
  - Baixa com `yt-dlp -f "bv*[height<=1080]+ba/b" -o {DATA_DIR}/{channel_id}/videos/%(id)s.%(ext)s <url>`
  - **Reencoda para formato uniforme** (mesmo codec/resolução/fps) via ffmpeg logo após o download, para evitar problemas de compatibilidade no concat demuxer. Ex: H.264 + AAC, resolução fixa (ex: 1920x1080), fps fixo (ex: 30).
  - Salva `file_path` no banco.
- Remove do disco e do banco vídeos que não estão mais entre os X mais recentes (mantém `video_count` sempre atualizado, evita crescimento infinito de disco).

### 7.3 Playlist builder (`playlist.py`)
- Gera `{DATA_DIR}/{channel_id}/playlist.txt` no formato:
  ```
  file '/data/canal-a/videos/VIDEOID1.mp4'
  file '/data/canal-a/videos/VIDEOID2.mp4'
  ```
- Chamado sempre que a lista de vídeos de um canal muda.

### 7.4 Stream manager (`stream_manager.py`)
- Mantém em memória um dict `{channel_id: subprocess.Popen}`.
- `start(channel_id)`:
  1. Garante que existe playlist válida (mínimo 1 vídeo).
  2. Sobe processo:
     ```
     ffmpeg -re -stream_loop -1 -f concat -safe 0 \
       -i {DATA_DIR}/{channel_id}/playlist.txt \
       -c:v copy -c:a copy \
       -f flv rtmp://a.rtmp.youtube.com/live2/{stream_key}
     ```
  3. Atualiza `status = 'running'`.
  4. Registra o processo para monitoramento (thread/task que espera o processo e reinicia automaticamente se ele morrer inesperadamente, com backoff).
- `stop(channel_id)`: envia SIGTERM ao processo, atualiza `status = 'stopped'`.
- `restart_with_new_playlist(channel_id)`: usado quando a lista de vídeos muda — stop + start, sem alterar `is_active`.

### 7.5 Scheduler (`scheduler.py`)
- Um job por canal ativo, no intervalo `check_interval_hours` do próprio canal (ou usa APScheduler com um job global que itera todos os canais ativos a cada X min e decide, por canal, se já passou do intervalo configurado).
- Fluxo do job: fetch → download novos → remove antigos fora da janela → se lista mudou, reconstrói playlist e chama `restart_with_new_playlist`.
- Ao iniciar o app (`main.py` startup), reativa automaticamente os canais com `is_active=1` (start dos processos ffmpeg) e agenda os jobs de checagem.

---

## 8. Dockerfile

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir yt-dlp

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/

VOLUME /data
EXPOSE 3000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3000"]
```

## 9. docker-compose.yml

```yaml
services:
  youtube-live-loop:
    build: .
    ports:
      - "3000:3000"
    volumes:
      - app-data:/data
    environment:
      APP_USERNAME: ${APP_USERNAME}
      APP_PASSWORD: ${APP_PASSWORD}
      SESSION_SECRET: ${SESSION_SECRET}
      YOUTUBE_API_KEY: ${YOUTUBE_API_KEY}
      DATA_DIR: /data
    restart: unless-stopped

volumes:
  app-data:
```

No Coolify: definir as envs (`APP_USERNAME`, `APP_PASSWORD`, `SESSION_SECRET`, `YOUTUBE_API_KEY`) no painel do recurso, garantir que o volume `/data` é persistente entre deploys.

---

## 10. Checklist de implementação (ordem sugerida)

1. Scaffold FastAPI + SQLite + templates básicos (sem lógica ainda)
2. Login/sessão com `APP_USERNAME`/`APP_PASSWORD`
3. CRUD de canais (form + tabela `channels`) — sem funcionalidade de stream ainda
4. `fetcher.py` — testar isoladamente que retorna IDs corretos de um canal real
5. `downloader.py` — baixar + reencodar 1 vídeo, validar arquivo resultante
6. `playlist.py` — gerar `playlist.txt` a partir da tabela `channel_videos`
7. `stream_manager.py` — testar start/stop manual de um processo ffmpeg com uma playlist de teste (pode usar uma RTMP key de teste/privado antes de ir pro canal real)
8. `scheduler.py` — automatizar o ciclo fetch → download → playlist → restart
9. Dashboard: exibir status real (running/stopped/error) puxando do `stream_manager`
10. Reativação automática dos canais ativos no startup do app
11. Dockerfile + docker-compose, testar deploy local (`docker compose up`)
12. Deploy no Coolify, configurar envs e volume persistente, testar com 2 canais simultâneos