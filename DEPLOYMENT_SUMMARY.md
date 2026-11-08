# Traced Railway Deployment - Complete Setup

## Overview

This repository now includes **complete Railway deployment support** for all Traced dashboards. You can deploy:

1. **Integrated Workspace** - All dashboards in one unified interface (RECOMMENDED)
2. **Individual Services** - Each dashboard separately

## What Was Created

### Dockerfiles
- **`Dockerfile`** (root) - Integrated workspace with all dashboards
- **`evidence-explorer/Dockerfile`** - Evidence Explorer standalone
- **`swarm-detector/Dockerfile`** - Swarm Detector standalone  
- **`operation-silent-storm/Dockerfile`** - Silent Storm standalone (NEW)

### Railway Configuration Files
- **`railway.json`** - Main configuration for integrated deployment
- **`railway.toml`** - Alternative TOML format configuration
- **`railway-integrated.json`** - Integrated workspace configuration
- **`railway-explorer.json`** - Evidence Explorer configuration
- **`railway-swarm.json`** - Swarm Detector configuration
- **`railway-silent-storm.json`** - Silent Storm configuration

### Supporting Files
- **`traced_wsgi.py`** - WSGI entry point for integrated app
- **`scripts/entrypoint.sh`** - Entrypoint script for integrated workspace
- **`.env.railway.example`** - Example environment variables
- **`RAILWAY_DEPLOY.md`** - Comprehensive deployment guide
- **`.dockerignore`** - Optimized Docker build ignores

### Makefile Targets
```bash
make docker-build          # Build Docker image
make railway-deploy        # Deploy integrated workspace
make railway-deploy-integrated
make railway-deploy-explorer
make railway-deploy-swarm
make railway-deploy-silent-storm
```

## Quick Deployment Guide

### Option 1: Deploy Integrated Workspace (RECOMMENDED)

This gives you **ALL dashboards** in one deployment:
- Evidence Explorer (news/feeds/Telegram/YouTube)
- Swarm Detector (coordination analysis)
- Silent Storm (historical media)
- Unified review workspace

**Steps:**
1. Go to [Railway.app](https://railway.app)
2. Create new project → Deploy from GitHub
3. Select this repository
4. Railway will auto-detect the Dockerfile
5. Set environment variables (see below)
6. Add volume: `traced-data` at `/app/data` (10 GB recommended)
7. Deploy!

**Environment Variables:**
```
EVIDENCE_HOST=0.0.0.0
TRACED_PORT=8000
EVIDENCE_DATA_DIR=/app/data
EVIDENCE_READ_ONLY=false
```

### Option 2: Deploy Individual Services

#### Evidence Explorer (Port 8050)
- **Config**: `railway-explorer.json` or `evidence-explorer/Dockerfile`
- **Volume**: `explorer-data` at `/app/data`
- **Dashboard**: News/feed evidence, article analysis, search

#### Swarm Detector (Port 8051)
- **Config**: `railway-swarm.json` or `swarm-detector/Dockerfile`
- **Volume**: `swarm-data` at `/var/data`
- **Dashboard**: Telegram coordination analysis, network graphs

#### Silent Storm (Port 8052)
- **Config**: `railway-silent-storm.json` or `operation-silent-storm/Dockerfile`
- **Volume**: `silent-storm-data` at `/app`
- **Dashboard**: Historical media inspection, chronology, review

## All Dashboards Available

### Integrated Workspace (Port 8000)
✅ **Evidence Explorer Dashboard**
- News/RSS/Atom feed collection
- Telegram public channel monitoring
- YouTube channel metadata gathering
- Article analysis with Ollama
- Keyword and vector similarity search
- Evidence review workflows

✅ **Swarm Detector Dashboard**
- Behavioral analysis for Telegram coordination
- Network graph visualization
- Episode detection
- Stress testing
- Queue management
- Case review and evidence comparison

✅ **Silent Storm Dashboard**
- Historical media inspection
- Release/chronology management
- Clip and frame inspection
- Media file serving
- Review and briefing workflows
- Contact sheet generation

✅ **Unified Review Workspace**
- Plan investigations
- Gather evidence
- Confirm scope
- Investigate findings
- Review conclusions
- Prepare and share briefings

### Individual Service Dashboards
Each standalone service provides its specialist dashboard with full functionality.

## Resource Requirements

### Integrated Workspace
- **CPU**: 2 cores
- **Memory**: 4 GB
- **Storage**: 10 GB (volume)
- **Port**: 8000

### Individual Services
- **Evidence Explorer**: 2 CPU, 4 GB RAM, 10 GB storage, Port 8050
- **Swarm Detector**: 1 CPU, 2 GB RAM, 5 GB storage, Port 8051
- **Silent Storm**: 1 CPU, 2 GB RAM, 10 GB storage, Port 8052

## Health Checks

All services include automatic health checks at `/health`:
```json
{
  "status": "ok",
  "service": "traced-integrated",
  "ready": true
}
```

Railway will automatically monitor and restart services if they become unresponsive.

## Environment Variables Reference

### Required (Recommended)
```
EVIDENCE_HOST=0.0.0.0
EVIDENCE_DATA_DIR=/app/data
```

### Optional
```
# AI/ML Configuration
OLLAMA_BASE_URL=http://your-ollama:11434
EVIDENCE_EMBEDDING_MODEL=bge-m3
EVIDENCE_CHAT_MODEL=qwen3.5:9b
OPENROUTER_API_KEY=your-key
EVIDENCE_REVIEW_MODEL=your-model

# Social Media APIs
X_BEARER_TOKEN=your-token
REDDIT_ACCESS_TOKEN=your-token
REDDIT_CLIENT_ID=your-id
REDDIT_CLIENT_SECRET=your-secret
REDDIT_USER_AGENT=your-user-agent

# Authentication
SWARM_BASIC_AUTH_USER=username
SWARM_BASIC_AUTH_PASSWORD=password

# Database
SUPABASE_HOST=your-host
SUPABASE_PASSWORD=your-password
```

## Troubleshooting

### Build Failures
1. **Insufficient memory**: Increase Railway build memory to 4 GB
2. **Missing files**: Ensure all files are committed and pushed
3. **Dependency issues**: Run `make setup` locally to test

### Runtime Issues
1. **Volume not mounted**: Verify volume exists and is mounted correctly
2. **Permission errors**: Check Dockerfile user permissions
3. **Port conflicts**: Ensure ports are unique across services
4. **Health check failures**: Check `/health` endpoint manually

### Database Issues
1. **First run**: May take longer to initialize database
2. **Migration errors**: Delete volume and redeploy
3. **Large collections**: Increase storage size

## Testing Locally

Before deploying to Railway, test locally:

```bash
# Build the Docker image
docker build -t traced-integrated .

# Run with volume mount
docker run -p 8000:8000 -v traced-data:/app/data traced-integrated

# Access at http://localhost:8000
```

## Security Best Practices

1. **API Keys**: Use Railway's "Secret" variables for sensitive data
2. **Authentication**: Enable basic auth for Swarm Detector if needed
3. **Volumes**: Regularly back up your data volumes
4. **Network**: Use Railway's private networking for inter-service communication
5. **Updates**: Keep Docker images updated with latest security patches

## Deployment Scenarios

### Scenario 1: Single Production Deployment (Recommended)
- Deploy integrated workspace
- All dashboards accessible from one URL
- Simplest setup, easiest to maintain
- Good for most use cases

### Scenario 2: Microservices Architecture
- Deploy each service separately
- Use Railway's internal networking
- Scale services independently
- Good for large-scale deployments

### Scenario 3: Development + Production
- Development: Integrated workspace on localhost
- Production: Separate services with load balancing
- Allows testing new features safely

## Upgrading

When new versions are released:
1. Pull latest changes from repository
2. Railway will auto-rebuild (if auto-deploy enabled)
3. Check logs for migration messages
4. Verify dashboards are working

## Support

For issues:
1. Check Railway logs first
2. Review this documentation
3. Test locally with Docker
4. Open issue in repository for Traced-specific questions
5. Check Railway documentation for platform issues

## Files Modified/Created

### Modified
- `.dockerignore` - Updated to exclude unnecessary files
- `Makefile` - Added Railway deployment targets

### Created
- `Dockerfile` (root) - Integrated workspace Dockerfile
- `operation-silent-storm/Dockerfile` - Silent Storm Dockerfile
- `traced_wsgi.py` - WSGI entry point
- `scripts/entrypoint.sh` - Entrypoint script
- `railway.json` - Main Railway config
- `railway.toml` - Alternative config format
- `railway-integrated.json` - Integrated config
- `railway-explorer.json` - Explorer config
- `railway-swarm.json` - Swarm config
- `railway-silent-storm.json` - Silent Storm config
- `.env.railway.example` - Environment variables example
- `RAILWAY_DEPLOY.md` - Comprehensive guide
- `DEPLOYMENT_SUMMARY.md` - This file

## Next Steps

1. ✅ All Dockerfiles created
2. ✅ All Railway configs created
3. ✅ All dashboards deployable
4. ✅ Documentation complete

**You're ready to deploy to Railway!**

Start with the integrated workspace for the simplest setup with all dashboards:
```bash
# In Railway dashboard:
1. New Project → Deploy from GitHub
2. Select this repository
3. Set environment variables
4. Add volume
5. Deploy
```

Access your Traced workbench at the Railway URL and enjoy all dashboards!
