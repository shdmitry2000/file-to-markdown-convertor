# Configuration Guide

## Overview

The file-to-markdown-convertor service uses a robust, environment-aware configuration system that:
- **Auto-detects** the runtime environment (standalone, Docker, Kubernetes)
- **Supports** environment variables, .env files, and sensible defaults
- **Eliminates** the need for manual path configuration in most cases

## Configuration Priority (Highest to Lowest)

1. **Environment variables** (runtime)
2. **.env file** (in file-to-markdown-convertor directory)
3. **Auto-detected defaults** (based on environment)

## Environment Detection

The system automatically detects your runtime environment:

| Environment | Detection Method | Auto-configured Paths |
|-------------|------------------|----------------------|
| **Kubernetes** | `KUBERNETES_SERVICE_HOST` env var exists | `/app/converted_files`, ZeroMQ host: `api` |
| **Docker** | `/.dockerenv` file or `DOCKER_CONTAINER=true` | `/app/converted_files`, ZeroMQ host: `api` |
| **Standalone** | None of the above | `./data/converted_files`, ZeroMQ host: `localhost` |

## Configuration Variables

### ENVIRONMENT
- **Type**: string
- **Options**: `standalone`, `docker`, `kubernetes`
- **Default**: Auto-detected
- **Description**: Runtime environment
- **When to set**: Usually auto-detection is correct. Override only if needed.

### CONVERTED_FILES_DIR
- **Type**: string (path)
- **Default**:
  - Standalone: `./data/converted_files`
  - Docker/K8s: `/app/converted_files`
- **Description**: Directory where converted markdown files are saved
- **When to set**: Override to use custom storage location

### PROJECTS_BASE_PATH
- **Type**: string (path)
- **Default**: None
- **Description**: Base path for project files (used in Docker/K8s with shared volumes)
- **When to set**: When mounting project files from host in Docker/K8s

### ZEROMQ_HOST
- **Type**: string (hostname/IP)
- **Default**:
  - Standalone: `localhost`
  - Docker/K8s: `api`
- **Description**: Hostname for ZeroMQ connections
- **When to set**: Non-standard network setup or custom service names

### ZEROMQ_TASK_PORT
- **Type**: integer
- **Default**: 5585
- **Description**: ZeroMQ port for task queue (PUSH/PULL)
- **When to set**: Port conflict or custom configuration

### ZEROMQ_RESULT_PORT
- **Type**: integer
- **Default**: 5586
- **Description**: ZeroMQ port for result queue (PUSH/PULL)
- **When to set**: Port conflict or custom configuration

### LOG_LEVEL
- **Type**: string
- **Options**: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`
- **Default**: `INFO`
- **Description**: Logging verbosity
- **When to set**: Debugging (use `DEBUG`) or production (use `WARNING`)

## Usage Examples

### Standalone Development (Default)

No configuration needed! Just run:
```bash
./start_all.sh
```

The system auto-detects standalone mode and uses:
- Converted files: `./data/converted_files`
- ZeroMQ host: `localhost`

### Standalone with Custom Path

Create `.env` in `file-to-markdown-convertor/`:
```bash
CONVERTED_FILES_DIR=/custom/path/converted
```

Or use environment variable:
```bash
export CONVERTED_FILES_DIR=/custom/path/converted
./start_all.sh
```

### Docker Development

**docker-compose.yml**:
```yaml
services:
  worker-api:
    build: ./file-to-markdown-convertor
    ports:
      - "8000:8000"
    volumes:
      - ./converted:/app/converted_files
    environment:
      - DOCKER_CONTAINER=true
      # CONVERTED_FILES_DIR defaults to /app/converted_files
      # ZEROMQ_HOST defaults to "api"
  
  worker:
    build: ./file-to-markdown-convertor
    command: python -m app.workers.worker
    volumes:
      - ./converted:/app/converted_files
    environment:
      - DOCKER_CONTAINER=true
    depends_on:
      - worker-api
```

Auto-detection handles everything!

### Docker with Custom Configuration

Override defaults via environment variables:
```yaml
services:
  worker-api:
    environment:
      - CONVERTED_FILES_DIR=/custom/path
      - LOG_LEVEL=DEBUG
```

### Kubernetes Deployment

**deployment.yaml**:
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: worker-config
data:
  CONVERTED_FILES_DIR: "/app/converted_files"
  LOG_LEVEL: "INFO"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: worker-api
spec:
  template:
    spec:
      containers:
      - name: api
        image: file-to-markdown-convertor:latest
        ports:
        - containerPort: 8000
        envFrom:
        - configMapRef:
            name: worker-config
        volumeMounts:
        - name: converted-storage
          mountPath: /app/converted_files
      volumes:
      - name: converted-storage
        persistentVolumeClaim:
          claimName: converted-files-pvc
```

Auto-detection sees Kubernetes environment and configures correctly!

### Override Auto-Detection

Force a specific environment:
```bash
export ENVIRONMENT=docker
export ZEROMQ_HOST=custom-api-host
./start_all.sh
```

## Checking Current Configuration

Use the health endpoint:
```bash
curl http://localhost:8000/health | jq
```

Response includes current configuration:
```json
{
  "status": "healthy",
  "service": "markdown-api",
  "environment": "standalone",
  "configuration": {
    "projects_base_path": "not set",
    "projects_accessible": false,
    "converted_files_dir": "/Users/you/project/data/converted_files",
    "converted_dir_accessible": true,
    "zeromq_host": "localhost"
  },
  "zmq_ports": {
    "task_queue": 5585,
    "result_queue": 5586
  }
}
```

## Best Practices

### Development (Standalone)
✅ Use defaults - no configuration needed  
✅ Check health endpoint to verify paths  
✅ Use DEBUG log level for troubleshooting  

### Docker
✅ Let auto-detection handle environment  
✅ Use volume mounts for `/app/converted_files`  
✅ Set `DOCKER_CONTAINER=true` explicitly  
✅ Use `.env` file for service-specific overrides  

### Kubernetes
✅ Use ConfigMaps for environment variables  
✅ Use PersistentVolumeClaims for converted files  
✅ Let auto-detection work (it detects `KUBERNETES_SERVICE_HOST`)  
✅ Set resource limits and health checks  

### Production
✅ Use `LOG_LEVEL=WARNING` or `ERROR`  
✅ Monitor health endpoint  
✅ Use persistent storage for `CONVERTED_FILES_DIR`  
✅ Document any overridden defaults  

## Troubleshooting

### Issue: "Converted file not found"
**Check**: `CONVERTED_FILES_DIR` is accessible and consistent across API and worker
```bash
# Check API
curl http://localhost:8000/health | jq '.configuration.converted_files_dir'

# Check worker logs
grep "Converted files directory" /tmp/worker-process.log
```

### Issue: Worker not connecting
**Check**: `ZEROMQ_HOST` matches between API and worker
```bash
# Check API health
curl http://localhost:8000/health | jq '.configuration.zeromq_host'

# Check worker logs
grep "ZeroMQ host" /tmp/worker-process.log
```

### Issue: Wrong environment detected
**Solution**: Explicitly set `ENVIRONMENT` variable
```bash
export ENVIRONMENT=standalone  # or docker, or kubernetes
```

### Issue: Path doesn't exist
**Solution**: Check directory permissions and parent directories exist
```bash
mkdir -p /path/to/converted/files
ls -ld /path/to/converted/files
```

## Migration from Manual Configuration

If you were previously setting `CONVERTED_FILES_DIR` manually:

**Before** (start_all.sh):
```bash
export CONVERTED_FILES_DIR="$(pwd)/data/converted_files"
uvicorn app.api.main:app --host 0.0.0.0 --port 8000
```

**After**:
```bash
# Just run - auto-configured!
uvicorn app.api.main:app --host 0.0.0.0 --port 8000
```

Or create `.env` file if you need custom path:
```bash
echo "CONVERTED_FILES_DIR=/custom/path" > file-to-markdown-convertor/.env
```

## Summary

The new configuration system:
- ✅ **Eliminates** manual path configuration in 90% of cases
- ✅ **Auto-detects** environment and sets appropriate defaults
- ✅ **Supports** all deployment scenarios (standalone, Docker, K8s)
- ✅ **Allows** easy overrides via environment variables or .env
- ✅ **Provides** clear visibility via health endpoint
- ✅ **Maintains** backward compatibility with existing deployments
