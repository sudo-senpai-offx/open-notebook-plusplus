# Docker Compose Installation (Recommended)

Multi-container setup with separate services. **Best for most users.**

> **Alternative Registry:** All images are available on both Docker Hub (`lfnovo/open_notebook`) and GitHub Container Registry (`ghcr.io/lfnovo/open-notebook`). Use GHCR if Docker Hub is blocked or you prefer GitHub-native workflows.

## Prerequisites

- **Docker Desktop** installed ([Download](https://www.docker.com/products/docker-desktop/))
- **5-10 minutes** of your time
- **API key** for at least one AI provider (OpenAI recommended for beginners)

## Step 1: Get docker-compose.yml (1 min)

**Option A: Download from repository**
```bash
curl -o docker-compose.yml https://raw.githubusercontent.com/lfnovo/open-notebook/main/docker-compose.yml
```

**Option B: Use the official file from the repo**

The official `docker-compose.yml` is in the root of our repository: [View on GitHub](https://github.com/lfnovo/open-notebook/blob/main/docker-compose.yml)

Copy that file to your project folder.

**Option C: Create manually**

Create a file called `docker-compose.yml` with this content:

```yaml
services:
  surrealdb:
    image: surrealdb/surrealdb:v2
    # Credentials default to root:root for a zero-config local setup. Before
    # exposing this instance to a network, set SURREAL_USER / SURREAL_PASSWORD
    # in a .env file (see .env.example) — they are applied here and to the
    # open_notebook service below, so the two always stay in sync.
    # List (exec) form so each interpolated value stays a single argument —
    # a password containing spaces would otherwise be split into several.
    command: ["start", "--log", "info", "--user", "${SURREAL_USER:-root}", "--pass", "${SURREAL_PASSWORD:-root}", "rocksdb:/mydata/mydatabase.db"]
    user: root  # Required for bind mounts on Linux
    ports:
      # Bound to localhost only: the open_notebook service reaches this over
      # the internal compose network regardless, so the host port is purely
      # for local debugging (e.g. Surrealist, `surreal sql`). Exposing this
      # on 0.0.0.0 would let anyone who can reach the host connect with the
      # default root:root credentials.
      - "127.0.0.1:8000:8000"
    volumes:
      - ./surreal_data:/mydata
    environment:
      - SURREAL_EXPERIMENTAL_GRAPHQL=true
    restart: always
    pull_policy: always

  open_notebook:
    image: lfnovo/open_notebook:v1-latest
    ports:
      - "8502:8502"  # Web UI
      - "5055:5055"  # REST API
    environment:
      # REQUIRED: Change this to your own secret string
      # This encrypts your API keys in the database
      - OPEN_NOTEBOOK_ENCRYPTION_KEY=change-me-to-a-secret-string

      # Database connection. SURREAL_USER / SURREAL_PASSWORD default to root:root
      # for local use; override them in a .env file before exposing the instance
      # (the same values configure the surrealdb service above).
      - SURREAL_URL=ws://surrealdb:8000/rpc
      - SURREAL_USER=${SURREAL_USER:-root}
      - SURREAL_PASSWORD=${SURREAL_PASSWORD:-root}
      - SURREAL_NAMESPACE=open_notebook
      - SURREAL_DATABASE=open_notebook
    volumes:
      - ./notebook_data:/app/data
    depends_on:
      - surrealdb
    restart: always
    pull_policy: always
```

**Edit the file:**
- Replace `change-me-to-a-secret-string` with your own secret (any string works, e.g., `my-super-secret-key-123`)
- (Optional) To use database credentials other than the default `root:root`, create a `.env` file next to `docker-compose.yml` with `SURREAL_USER=...` and `SURREAL_PASSWORD=...` — both services pick them up automatically ([.env.example](https://github.com/lfnovo/open-notebook/blob/main/.env.example) shows the full format)

---

## Step 2: Start Services (2 min)

Open terminal in the `open-notebook` folder:

```bash
docker compose up -d
```

Wait 15-20 seconds for all services to start:
```
✅ surrealdb running on :8000
✅ open_notebook running on :8502 (UI) and :5055 (API)
```

Check status:
```bash
docker compose ps
```

---

## Step 3: Verify Installation (1 min)

**API Health:**
```bash
curl http://localhost:5055/health
# Should return: {"status": "healthy"}
```

**Frontend Access:**
Open browser to:
```
http://localhost:8502
```

You should see the Open Notebook interface!

---

## Step 4: Configure AI Provider (2 min)

1. Go to **Manage** → **Models**
2. Click **Add Credential**
3. Select your provider (e.g., OpenAI, Anthropic, Google)
4. Give it a name, paste your API key
5. Click **Save**
6. Click **Test Connection** — should show success
7. Click **Discover Models** → **Register Models**

Your models are now available!

> **Need an API key?** Get one from your chosen provider:
> - **OpenAI**: https://platform.openai.com/api-keys
> - **Anthropic**: https://console.anthropic.com/
> - **Google**: https://aistudio.google.com/
> - **Groq**: https://console.groq.com/

---

## Step 5: First Notebook (2 min)

1. Click **New Notebook**
2. Name: "My Research"
3. Description: "Getting started"
4. Click **Create**

Done! You now have a fully working Open Notebook instance.

---

## Configuration

### Adding Ollama (Free Local Models)

Instead of manually editing, use our ready-made example:

```bash
# Download the Ollama example
curl -o docker-compose.yml https://raw.githubusercontent.com/lfnovo/open-notebook/main/examples/docker-compose-ollama.yml

# Or copy from repo
cp examples/docker-compose-ollama.yml docker-compose.yml
```

See [examples/docker-compose-ollama.yml](../../examples/docker-compose-ollama.yml) for the complete setup.

**Manual setup:** Add this to your existing `docker-compose.yml`:

```yaml
  ollama:
    image: ollama/ollama:latest
    ports:
      - "11434:11434"
    volumes:
      - ollama_models:/root/.ollama
    restart: always

volumes:
  ollama_models:
```

Then restart and pull a model:
```bash
docker compose restart
docker exec open-notebook-local-ollama-1 ollama pull mistral
```

Configure Ollama in the Settings UI:
1. Go to **Manage** → **Models**
2. Click **Add Credential** → Select **Ollama**
3. Enter base URL: `http://ollama:11434`
4. Click **Save**, then **Test Connection**
5. Click **Discover Models** → **Register Models**

---

## Environment Variables Reference

| Variable | Purpose | Example |
|----------|---------|---------|
| `OPEN_NOTEBOOK_ENCRYPTION_KEY` | Encryption key for credentials | `my-secret-key` |
| `SURREAL_URL` | Database connection | `ws://surrealdb:8000/rpc` |
| `SURREAL_USER` | Database user | `root` |
| `SURREAL_PASSWORD` | Database password | `root` |
| `SURREAL_NAMESPACE` | Database namespace | `open_notebook` |
| `SURREAL_DATABASE` | Database name | `open_notebook` |
| `API_URL` | API external URL | `http://localhost:5055` |
| `OPEN_NOTEBOOK_EMBEDDING_BATCH_SIZE` | Override embedding batch size for stricter/local providers (recommended: `8` for CPU-only local setups) | `50` |

See [Environment Reference](../5-CONFIGURATION/environment-reference.md) for complete list.

---

## Common Tasks

### Stop Services
```bash
docker compose down
```

### View Logs
```bash
# All services
docker compose logs -f

# Specific service
docker compose logs -f api
```

### Restart Services
```bash
docker compose restart
```

### Update to Latest Version
```bash
docker compose down
docker compose pull
docker compose up -d
```

### Remove All Data
```bash
docker compose down -v
```

---

## Troubleshooting

### "Cannot connect to API" Error

1. Check if Docker is running:
```bash
docker ps
```

2. Check if services are running:
```bash
docker compose ps
```

3. Check API logs:
```bash
docker compose logs api
```

4. Wait longer - services can take 20-30 seconds to start on first run

---

### Port Already in Use

If you get "Port 8502 already in use", change the port:

```yaml
ports:
  - "8503:8502"  # Use 8503 instead
  - "5055:5055"  # Keep API port same
```

Then access at `http://localhost:8503`

---

### Credential Issues

1. Go to **Manage** → **Models**
2. Click **Test Connection** on the credential
3. If it fails, verify key at provider's website
4. Check you have credits in your account
5. Delete and re-create the credential if needed

---

### Database Connection Issues

Check SurrealDB is running:
```bash
docker compose logs surrealdb
```

Reset database:
```bash
docker compose down -v
docker compose up -d
```

### Database Permission Denied (Linux)

If you see `Permission denied` or `Failed to create RocksDB directory` in SurrealDB logs:

```bash
docker compose logs surrealdb | grep -i permission
```

This happens because SurrealDB runs as a non-root user but Docker creates bind mount directories as root. Add `user: root` to the surrealdb service:

```yaml
surrealdb:
  image: surrealdb/surrealdb:v2
  user: root  # Fix for Linux bind mount permissions
  # ... rest of config
```

Then restart:
```bash
docker compose down -v
docker compose up -d
```

---

## Alternative Setups

Looking for different configurations? Check out our [examples/](../../examples/) folder:

- **[Ollama Setup](../../examples/docker-compose-ollama.yml)** - Run local AI models (free, private)
- **[Single Container](../../examples/docker-compose-single.yml)** - All-in-one container (deprecated, will be removed in v2)
- **[Development](../../examples/docker-compose-dev.yml)** - For contributors and developers

Each example includes detailed comments and usage instructions.

---

## Next Steps

1. **Add Content**: Sources, notebooks, documents
2. **Configure Models**: Manage → Models (choose your preferences)
3. **Explore Features**: Chat, search, transformations
4. **Read Guide**: [User Guide](../3-USER-GUIDE/index.md)

---

## Production Deployment

For production use, see:
- [Security Hardening](../5-CONFIGURATION/security.md)
- [Reverse Proxy](../5-CONFIGURATION/reverse-proxy.md)

---

## Deploy the upgraded build on another PC

> Verified end-to-end for the upgraded offline-first build (v1.15.0): every
> committed runtime feature (job lifecycle B1–B3, auth guard B4, self-hosted
> notifications D1) ships in the image, all opt-in flags default **OFF**, and
> the DB persists on the `./surreal_data` bind mount.

### Path A — machine has internet (and wants to build)

```bash
# 1. Get the repo at the locked HEAD
git clone <repo-url> open-notebook && cd open-notebook
git checkout <committed-deploy-tag-or-commit>

# 2. Config from the shipped example (defaults are offline-first: all opt-in flags OFF)
cp .env.example .env
#   - set OPEN_NOTEBOOK_ENCRYPTION_KEY to a random 32+ char string (required to decrypt sources)
#   - leave CLOUD_ALLOWED / USE_MIXTURE / USE_RERANKER_BEFORE_TRUNCATE at their default OFF
#     unless you deliberately enabled them in your probe runs

# 3. Build from HEAD and start
docker compose up -d --build

# 4. Re-apply the ADR-009 resource envelope (compose sets NO limits by itself)
docker update --cpus 2  --memory 2g    --memory-swap 2g    open-notebook-open_notebook-1
docker update --cpus 1  --memory 512m  --memory-swap 512m  open-notebook-surrealdb-1
#    ⚠ `--memory` alone fails ("Memory limit should be smaller than already set
#    memoryswap limit") — always pass `--memory-swap` together with `--memory`.
```

### Path B — machine has no internet / no build toolchain (portable image)

```bash
# On the source PC (where the image was built from HEAD):
docker save open-notebook-open_notebook:latest surrealdb/surrealdb:v2 \
  -o open-notebook-v1.15.0-images.tar
# ~0.8 GB (measured): docker save shares the layers the two images have in
# common, and the layers are already compressed, so gzip adds little.
# Copy the tar to the target PC.

# On the target PC (has Docker, no internet needed):
docker load -i open-notebook-v1.15.0-images.tar
cp .env.example .env            # set OPEN_NOTEBOOK_ENCRYPTION_KEY as above
docker compose -f docker-compose.yml -f docker-compose.offline.yml up -d --no-build
docker update --cpus 2 --memory 2g --memory-swap 2g     open-notebook-open_notebook-1
docker update --cpus 1 --memory 512m --memory-swap 512m open-notebook-surrealdb-1
# docker-compose.offline.yml pins both services to pull_policy: never, so the
# pre-loaded images are used without any pull attempt; --no-build skips the
# (still declared) `build:` key on the app service.
```

### Verification checklist (all of these passed on the deployed stack)

```bash
curl -s http://localhost:5055/health                       # {"status":"healthy"}
curl -s -L -o /dev/null -w "%{http_code}\n" http://localhost:8502/   # 200 -> /notebooks
curl -s http://localhost:5055/api/notebooks -o /dev/null -w "%{http_code}\n"  # 200
curl -s http://localhost:5055/api/capabilities             # docling_available: true
# runtime flags must be OFF (defaults enforced)
docker exec open-notebook-open_notebook-1 python3 -c "from open_notebook.ai.model_policy import get_policy_config as g; c=g(); print({k:c[k] for k in ('CLOUD_ALLOWED','USE_ROUTER','USE_MIXTURE','USE_RERANKER_BEFORE_TRUNCATE')})"
# data intact (blocking client; this build's venv returns the list directly, not {result:[...]})
docker exec open-notebook-open_notebook-1 /app/.venv/bin/python3 -c "
import os
from surrealdb import Surreal
db = Surreal(os.environ['SURREAL_URL']); db.signin({'user':'root','pass':'root'}); db.use('open_notebook','open_notebook')
print('notebooks =', db.query('SELECT count() AS n FROM notebook GROUP ALL')[0]['n'])
db.close()"
```

---

## Getting Help

- **Discord**: [Community support](https://discord.gg/37XJPXfz2w)
- **Issues**: [GitHub Issues](https://github.com/lfnovo/open-notebook/issues)
- **Docs**: [Full documentation](../index.md)
