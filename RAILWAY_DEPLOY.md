# Railway Deployment Guide for Traced

This guide explains how to deploy the Traced evidence workbench to Railway with all dashboards enabled.

## Quick Start

### Option 1: Deploy the Integrated Workspace (Recommended)

This deploys **ALL dashboards** in a single unified interface:

1. **Create a new Railway project**
   - Go to [Railway.app](https://railway.app) and sign in
   - Click "New Project" → "Deploy from GitHub repo"
   - Select this repository

2. **Configure the deployment**
   - Use the `Dockerfile` in the root directory
   - Set the following environment variables:
     ```
     EVIDENCE_HOST=0.0.0.0
     TRACED_PORT=8000
     EVIDENCE_DATA_DIR=/app/data
     EVIDENCE_READ_ONLY=false
     ```

3. **Add persistent storage**
   - Create a volume named `traced-data` mounted at `/app/data`
   - Recommended size: 10 GB (adjust based on your evidence collection size)

4. **Deploy**
   - Railway will build the Docker image and start the service
   - Access your dashboard at the provided Railway URL

### Option 2: Deploy Individual Services

If you prefer separate deployments for each dashboard:

#### Evidence Explorer (Port 8050)
- **Configuration**: Use `railway-explorer.json` or `evidence-explorer/Dockerfile`
- **Dashboard**: News/feed evidence collection, analysis, and review
- **Volume**: `explorer-data` at `/app/data`

#### Swarm Detector (Port 8051)
- **Configuration**: Use `railway-swarm.json` or `swarm-detector/Dockerfile`
- **Dashboard**: Telegram coordination behavioral analysis
- **Volume**: `swarm-data` at `/var/data`
- **Optional**: Set `SWARM_BASIC_AUTH_USER` and `SWARM_BASIC_AUTH_PASSWORD` for authentication

#### Silent Storm (Port 8052)
- **Configuration**: Use `railway-silent-storm.json` or `operation-silent-storm/Dockerfile`
- **Dashboard**: Historical media inspection and review
- **Volume**: `silent-storm-data` at `/app`

## Environment Variables

### Common Variables (All Services)

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `EVIDENCE_HOST` | Host to bind to | `0.0.0.0` | No |
| `EVIDENCE_DATA_DIR` | Path to data directory | `/app/data` | No |
| `EVIDENCE_READ_ONLY` | Enable read-only mode | `false` | No |
| `PYTHONUNBUFFERED` | Python buffered output | `1` | No |
| `PYTHONDONTWRITEBYTECODE` | Disable .pyc files | `1` | No |

### Evidence Explorer Specific

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `EVIDENCE_PORT` | Port to listen on | `8050` | No |
| `OLLAMA_BASE_URL` | Ollama API URL | `""` (disabled) | No |
| `EVIDENCE_EMBEDDING_MODEL` | Embedding model name | `bge-m3` | No |
| `EVIDENCE_CHAT_MODEL` | Chat/analysis model | `qwen3.5:9b` | No |
| `OPENROUTER_API_KEY` | OpenRouter API key | `""` | No |
| `EVIDENCE_REVIEW_MODEL` | Review model | `""` | No |
| `X_BEARER_TOKEN` | X/Twitter API token | `""` | No |
| `REDDIT_ACCESS_TOKEN` | Reddit access token | `""` | No |
| `REDDIT_CLIENT_ID` | Reddit client ID | `""` | No |
| `REDDIT_CLIENT_SECRET` | Reddit client secret | `""` | No |
| `REDDIT_USER_AGENT` | Reddit user agent | `""` | No |

### Swarm Detector Specific

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `PORT` | Port to listen on | `8050` | No |
| `SWARM_DATA_DIR` | Data directory | `/var/data` | No |
| `SWARM_BASIC_AUTH_USER` | Basic auth username | `""` (disabled) | No |
| `SWARM_BASIC_AUTH_PASSWORD` | Basic auth password | `""` (disabled) | No |

### Silent Storm Specific

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `PORT` | Port to listen on | `8052` | No |
| `SILENT_STORM_DB_PATH` | Database path | `/app/silent-storm.db` | No |
| `SILENT_STORM_DATASET_ROOT` | Dataset root | `/app/dataset` | No |

## Railway Configuration Files

This repository includes ready-to-use Railway configuration files:

- **`railway-integrated.json`** - Complete integrated workspace with all dashboards
- **`railway-explorer.json`** - Evidence Explorer standalone
- **`railway-swarm.json`** - Swarm Detector standalone
- **`railway-silent-storm.json`** - Silent Storm standalone

To use these files:
1. Import the JSON into Railway when creating a new service
2. Railway will automatically configure the deployment

## Resource Recommendations

### Integrated Workspace
- **CPU**: 2 cores
- **Memory**: 4 GB
- **Storage**: 10 GB (for data volume)

### Individual Services
- **Evidence Explorer**: 2 CPU, 4 GB RAM, 10 GB storage
- **Swarm Detector**: 1 CPU, 2 GB RAM, 5 GB storage
- **Silent Storm**: 1 CPU, 2 GB RAM, 10 GB storage

## Database Initialization

The first deployment will automatically:
1. Create the SQLite database at the specified path
2. Apply all necessary migrations
3. Seed with demo data (if applicable)

For production use, you may want to:
- Import your existing evidence collections
- Configure external providers (Ollama, OpenRouter, etc.)
- Set up authentication if needed

## Accessing the Dashboards

### Integrated Workspace
- **URL**: Your Railway service URL (e.g., `https://traced-integrated.up.railway.app`)
- **Port**: 8000 (or as configured)
- **Features**: All dashboards accessible through unified navigation

### Individual Services
- **Evidence Explorer**: Port 8050
- **Swarm Detector**: Port 8051
- **Silent Storm**: Port 8052

## Health Checks

All services include health check endpoints at `/health` that return:
```json
{
  "status": "ok",
  "ready": true,
  "releases": <count>
}
```

Railway will automatically monitor these endpoints and restart the service if it becomes unresponsive.

## Troubleshooting

### Build Failures
- Ensure you have at least 4 GB of memory allocated for the build
- Check that the Docker build context includes all necessary files
- Verify Python 3.12 is available

### Runtime Issues
- Check the Railway logs for error messages
- Verify volume mounts are correctly configured
- Ensure environment variables are set correctly
- Test locally with Docker before deploying to Railway

### Database Issues
- The first run may take longer as it initializes the database
- If you see migration errors, try deleting the database volume and redeploying
- For large evidence collections, consider increasing the storage size

## Local Testing

Before deploying to Railway, test locally:

```bash
# Build the Docker image
docker build -t traced-integrated .

# Run with volume mount
docker run -p 8000:8000 -v traced-data:/app/data traced-integrated

# Access at http://localhost:8000
```

## Security Considerations

1. **Authentication**: Swarm Detector supports basic authentication. Enable it for private deployments.
2. **Data Protection**: All data is stored in persistent volumes. Ensure proper backups.
3. **API Keys**: Never commit API keys to the repository. Use Railway's environment variable management.
4. **Network Access**: By default, services bind to `0.0.0.0`. For private deployments, consider using Railway's private networking.

## Updating Deployments

When new versions are released:
1. Pull the latest changes from the repository
2. Railway will automatically rebuild and redeploy (if auto-deploy is enabled)
3. Check the Railway logs for any migration messages
4. Verify the dashboards are working correctly

## Custom Configuration

For advanced configurations:
1. Create a custom Dockerfile based on the provided ones
2. Add additional environment variables as needed
3. Configure custom volumes and resource limits
4. Set up custom domains and SSL certificates in Railway

## Support

For issues with Railway deployments:
- Check the [Railway Documentation](https://docs.railway.app/)
- Review the Railway community forums
- Open an issue in this repository for Traced-specific questions

## License

The Traced applications are licensed under their respective licenses (see each package's README). Railway deployment configurations are provided as-is for convenience.
