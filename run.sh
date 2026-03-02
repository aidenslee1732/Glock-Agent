#!/bin/bash
# Glock Local Development Runner
# Usage: ./run.sh [command]
#
# Commands:
#   dev       - Start in DEV MODE (no Redis/DB/Auth required!)
#   server    - Start the gateway server (requires Redis/DB)
#   test      - Run tests
#   setup     - Set up local development environment
#   docker    - Start with Docker Compose
#   clean     - Clean up local data
#   help      - Show this help

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"

echo -e "${BLUE}"
echo "  ██████╗ ██╗      ██████╗  ██████╗██╗  ██╗"
echo " ██╔════╝ ██║     ██╔═══██╗██╔════╝██║ ██╔╝"
echo " ██║  ███╗██║     ██║   ██║██║     █████╔╝ "
echo " ██║   ██║██║     ██║   ██║██║     ██╔═██╗ "
echo " ╚██████╔╝███████╗╚██████╔╝╚██████╗██║  ██╗"
echo "  ╚═════╝ ╚══════╝ ╚═════╝  ╚═════╝╚═╝  ╚═╝"
echo -e "${NC}"
echo "Model B - Client Orchestrated Architecture"
echo ""

# Check if .env exists
check_env() {
    if [ ! -f "$PROJECT_DIR/.env" ]; then
        echo -e "${YELLOW}Warning: .env file not found${NC}"
        echo "Creating from .env.example..."
        if [ -f "$PROJECT_DIR/.env.example" ]; then
            cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
            echo -e "${GREEN}Created .env file. Please edit it with your settings.${NC}"
        else
            echo -e "${RED}Error: .env.example not found${NC}"
            return 1
        fi
    fi
    source "$PROJECT_DIR/.env" 2>/dev/null || true
}

# Set up virtual environment
setup_venv() {
    if [ ! -d "$VENV_DIR" ]; then
        echo "Creating virtual environment..."
        python3 -m venv "$VENV_DIR"
    fi

    echo "Activating virtual environment..."
    source "$VENV_DIR/bin/activate"
}

# Install dependencies
install_deps() {
    echo "Installing dependencies..."
    pip install --upgrade pip

    # Install from requirements.txt
    if [ -f "$PROJECT_DIR/requirements.txt" ]; then
        pip install -r "$PROJECT_DIR/requirements.txt"
    fi

    # Install shared protocol
    if [ -d "$PROJECT_DIR/packages/shared_protocol" ]; then
        pip install -e "$PROJECT_DIR/packages/shared_protocol"
    fi

    # Install server
    if [ -d "$PROJECT_DIR/apps/server" ]; then
        pip install -e "$PROJECT_DIR/apps/server"
    fi

    # Install CLI
    if [ -d "$PROJECT_DIR/apps/cli" ]; then
        pip install -e "$PROJECT_DIR/apps/cli"
    fi
}

# Start local services (Redis, PostgreSQL)
start_local_services() {
    echo "Checking local services..."

    # Check Redis
    if ! command -v redis-cli &> /dev/null; then
        echo -e "${YELLOW}Redis not found. Install with: brew install redis${NC}"
    elif ! redis-cli ping &> /dev/null; then
        echo "Starting Redis..."
        if [[ "$OSTYPE" == "darwin"* ]]; then
            brew services start redis 2>/dev/null || redis-server --daemonize yes
        else
            redis-server --daemonize yes
        fi
        sleep 1
    fi

    # Check PostgreSQL
    if ! command -v psql &> /dev/null; then
        echo -e "${YELLOW}PostgreSQL not found. Install with: brew install postgresql${NC}"
    elif ! psql -h localhost -U postgres -c "SELECT 1" &> /dev/null 2>&1; then
        echo -e "${YELLOW}PostgreSQL not running or not accessible${NC}"
        echo "Start with: brew services start postgresql"
    fi
}

# Run database migrations
run_migrations() {
    echo "Running database migrations..."

    if [ -z "$DATABASE_URL" ]; then
        echo -e "${YELLOW}DATABASE_URL not set, using default${NC}"
        DATABASE_URL="postgresql://postgres:postgres@localhost:5432/glock"
    fi

    # Create database if not exists
    psql "$DATABASE_URL" -c "SELECT 1" &> /dev/null 2>&1 || {
        echo "Creating database..."
        createdb glock 2>/dev/null || true
    }

    # Run migrations
    for migration in "$PROJECT_DIR"/infra/supabase/migrations/*.sql; do
        if [ -f "$migration" ]; then
            echo "  Running $(basename "$migration")..."
            psql "$DATABASE_URL" -f "$migration" 2>/dev/null || true
        fi
    done

    echo -e "${GREEN}Migrations complete${NC}"
}

# Start in DEV MODE (no Redis/DB/Auth required)
start_dev_mode() {
    setup_venv

    # Install dependencies if not already installed
    if ! python -c "import redis" 2>/dev/null; then
        echo "Installing dependencies..."
        install_deps
    fi

    check_env

    echo -e "${GREEN}Starting in DEV MODE (no Redis/DB/Auth)${NC}"
    echo ""

    # Set dev mode flags
    export DEV_MODE=1
    export MOCK_REDIS=1
    export MOCK_DB=1
    export SKIP_AUTH=1

    # Add project root to PYTHONPATH so imports work
    export PYTHONPATH="$PROJECT_DIR:$PYTHONPATH"

    # Minimal config
    export JWT_SECRET="${JWT_SECRET:-dev-mode-secret-key-at-least-32-chars}"
    export JWT_ISSUER="${JWT_ISSUER:-glock.dev}"
    export CONTEXT_MASTER_KEY="${CONTEXT_MASTER_KEY:-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef}"
    export LOG_LEVEL="${LOG_LEVEL:-error}"

    if [ -z "$ANTHROPIC_API_KEY" ]; then
        echo -e "${YELLOW}Warning: ANTHROPIC_API_KEY not set${NC}"
        echo "LLM requests will fail without an API key."
        echo "Set it with: export ANTHROPIC_API_KEY=sk-ant-..."
        echo ""
    fi

    echo "Configuration:"
    echo "  DEV_MODE: enabled (no Redis/DB required)"
    echo "  MOCK_REDIS: in-memory store"
    echo "  MOCK_DB: in-memory store"
    echo "  SKIP_AUTH: authentication disabled"
    echo "  ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:+set}${ANTHROPIC_API_KEY:-NOT SET}"
    echo ""
    echo -e "${YELLOW}Note: This mode is for development only!${NC}"
    echo ""

    cd "$PROJECT_DIR"
    # Only reload on server code changes, not when user projects are modified
    # This prevents session loss when Glock writes files to user workspaces
    uvicorn apps.server.src.main:app --host 0.0.0.0 --port 8000 --reload \
        --reload-dir apps/server/src \
        --reload-dir packages/shared_protocol
}

# Start the server (requires Redis/DB)
start_server() {
    setup_venv

    # Install dependencies if not already installed
    if ! python -c "import redis" 2>/dev/null; then
        echo "Installing dependencies..."
        install_deps
    fi

    check_env

    # Add project root to PYTHONPATH so imports work
    export PYTHONPATH="$PROJECT_DIR:$PYTHONPATH"

    # Set defaults if not in .env
    export DATABASE_URL="${DATABASE_URL:-postgresql://postgres:postgres@localhost:5432/glock}"
    export REDIS_URL="${REDIS_URL:-redis://localhost:6379}"
    export JWT_SECRET="${JWT_SECRET:-local-dev-secret-key-at-least-32-chars}"
    export JWT_ISSUER="${JWT_ISSUER:-glock.dev}"
    export CONTEXT_MASTER_KEY="${CONTEXT_MASTER_KEY:-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef}"
    export LOG_LEVEL="${LOG_LEVEL:-error}"

    if [ -z "$ANTHROPIC_API_KEY" ]; then
        echo -e "${YELLOW}Warning: ANTHROPIC_API_KEY not set${NC}"
        echo "LLM requests will fail without an API key."
        echo ""
    fi

    echo -e "${GREEN}Starting Glock Gateway Server...${NC}"
    echo ""
    echo "Configuration:"
    echo "  DATABASE_URL: $DATABASE_URL"
    echo "  REDIS_URL: $REDIS_URL"
    echo "  LOG_LEVEL: $LOG_LEVEL"
    echo ""

    cd "$PROJECT_DIR"
    # Only reload on server code changes, not when user projects are modified
    uvicorn apps.server.src.main:app --host 0.0.0.0 --port 8000 --reload \
        --reload-dir apps/server/src \
        --reload-dir packages/shared_protocol
}

# Run tests
run_tests() {
    setup_venv
    check_env

    echo -e "${GREEN}Running tests...${NC}"
    echo ""

    cd "$PROJECT_DIR"

    # Set test environment
    export TESTING=1
    export JWT_SECRET="test-secret-key-at-least-32-characters"
    export CONTEXT_MASTER_KEY="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

    # Run pytest
    if [ "$1" == "--coverage" ]; then
        pytest tests/ -v --cov=apps --cov-report=html --cov-report=term
        echo ""
        echo -e "${GREEN}Coverage report: htmlcov/index.html${NC}"
    else
        pytest tests/ -v "$@"
    fi
}

# Set up development environment
setup_dev() {
    echo -e "${GREEN}Setting up development environment...${NC}"
    echo ""

    setup_venv
    install_deps

    check_env
    start_local_services
    run_migrations

    echo ""
    echo -e "${GREEN}Setup complete!${NC}"
    echo ""
    echo "Next steps:"
    echo "  1. Edit .env with your ANTHROPIC_API_KEY"
    echo "  2. Run: ./run.sh server"
    echo "  3. In another terminal, test with CLI"
}

# Start with Docker
start_docker() {
    check_env

    echo -e "${GREEN}Starting with Docker Compose...${NC}"

    if [ -z "$ANTHROPIC_API_KEY" ]; then
        echo -e "${YELLOW}Warning: ANTHROPIC_API_KEY not set${NC}"
        read -p "Enter your Anthropic API key: " ANTHROPIC_API_KEY
        export ANTHROPIC_API_KEY
    fi

    docker-compose up -d --build

    echo ""
    echo "Waiting for services to start..."
    sleep 5

    # Check health
    if curl -s http://localhost:8000/health | grep -q "healthy" 2>/dev/null; then
        echo -e "${GREEN}Gateway is healthy!${NC}"
    else
        echo -e "${YELLOW}Gateway may not be ready yet${NC}"
        echo "Check logs with: docker-compose logs gateway"
    fi

    echo ""
    echo "Services:"
    echo "  Gateway: http://localhost:8000"
    echo "  PostgreSQL: localhost:5432"
    echo "  Redis: localhost:6379"
    echo ""
    echo "Commands:"
    echo "  View logs: docker-compose logs -f"
    echo "  Stop: docker-compose down"
}

# Clean up
clean() {
    echo -e "${YELLOW}Cleaning up...${NC}"

    # Stop Docker
    docker-compose down 2>/dev/null || true

    # Remove virtual environment
    rm -rf "$VENV_DIR"

    # Remove local data
    rm -rf "$PROJECT_DIR/.glock"
    rm -rf "$PROJECT_DIR/htmlcov"
    rm -rf "$PROJECT_DIR/.pytest_cache"

    # Clean Python cache
    find "$PROJECT_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    find "$PROJECT_DIR" -type f -name "*.pyc" -delete 2>/dev/null || true

    echo -e "${GREEN}Clean complete${NC}"
}

# Show help
show_help() {
    echo "Usage: ./run.sh [command]"
    echo ""
    echo "Commands:"
    echo "  dev             ${GREEN}Start in DEV MODE (no Redis/DB/Auth needed!)${NC}"
    echo "  server          Start the gateway server (requires Redis/DB)"
    echo "  cli             Start the CLI/TUI client"
    echo "  test            Run all tests"
    echo "  test --coverage Run tests with coverage report"
    echo "  setup           Set up local development environment"
    echo "  docker          Start everything with Docker Compose"
    echo "  migrate         Run database migrations"
    echo "  clean           Clean up local data and cache"
    echo "  help            Show this help"
    echo ""
    echo "Quick Start (Dev Mode):"
    echo "  export ANTHROPIC_API_KEY=sk-ant-..."
    echo "  ./run.sh dev"
    echo ""
    echo "Examples:"
    echo "  ./run.sh dev            # Quick start - no dependencies"
    echo "  ./run.sh setup          # Full setup with Redis/DB"
    echo "  ./run.sh server         # Start with full infrastructure"
    echo "  ./run.sh test           # Run tests"
    echo "  ./run.sh docker         # Use Docker instead"
}

# Start CLI/TUI client
start_cli() {
    setup_venv

    # Install dependencies if not already installed
    if ! python -c "import textual" 2>/dev/null; then
        echo "Installing CLI dependencies..."
        install_deps
    fi

    echo -e "${GREEN}Starting Glock CLI...${NC}"
    echo ""

    export PYTHONPATH="$PROJECT_DIR:$PYTHONPATH"
    cd "$PROJECT_DIR"
    
    # Check if --server argument is already provided
    local has_server_arg=false
    for arg in "$@"; do
        if [[ "$arg" == "--server" ]] || [[ "$arg" == "--server="* ]]; then
            has_server_arg=true
            break
        fi
    done
    
    # If no --server argument provided, add default localhost WebSocket server
    if [[ "$has_server_arg" == false ]]; then
        echo "Using default server: ws://localhost:8000"
        python -m apps.cli.src.cli --server "ws://localhost:8000" "${@}"
    else
        python -m apps.cli.src.cli "${@}"
    fi
}

# Main command handler
case "${1:-help}" in
    dev)
        start_dev_mode
        ;;
    server|start)
        start_server
        ;;
    cli|tui)
        shift
        start_cli "$@"
        ;;
    test|tests)
        shift
        run_tests "$@"
        ;;
    setup|install)
        setup_dev
        ;;
    docker)
        start_docker
        ;;
    migrate|migrations)
        check_env
        run_migrations
        ;;
    clean)
        clean
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        echo -e "${RED}Unknown command: $1${NC}"
        echo ""
        show_help
        exit 1
        ;;
esac
