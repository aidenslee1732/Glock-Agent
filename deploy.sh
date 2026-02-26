#!/bin/bash
# Glock Deployment Script - Model B Architecture
set -e

echo "🔒 Glock Deployment Script"
echo "=========================="
echo ""

# Check for required tools
check_tool() {
    if ! command -v $1 &> /dev/null; then
        echo "❌ $1 is not installed. Please install it first."
        exit 1
    fi
    echo "✓ $1 found"
}

# Deployment target
TARGET="${1:-railway}"

case $TARGET in
    railway)
        echo "📦 Deploying to Railway..."
        echo ""

        check_tool railway

        # Check if logged in
        if ! railway whoami &> /dev/null; then
            echo "Please log in to Railway first:"
            railway login
        fi

        # Check if project is linked
        if ! railway status &> /dev/null; then
            echo ""
            echo "No Railway project linked. Options:"
            echo "  1. Link to existing project: railway link"
            echo "  2. Create new project: railway init"
            echo ""
            read -p "Create new project? (y/n) " -n 1 -r
            echo
            if [[ $REPLY =~ ^[Yy]$ ]]; then
                railway init
            else
                echo "Please run 'railway link' to link to an existing project"
                exit 1
            fi
        fi

        echo ""
        echo "Setting up environment variables..."
        echo "Please ensure these are set in Railway dashboard:"
        echo "  - DATABASE_URL (from Railway Postgres plugin)"
        echo "  - REDIS_URL (from Railway Redis plugin)"
        echo "  - JWT_SECRET (generate with: openssl rand -hex 32)"
        echo "  - ANTHROPIC_API_KEY (your API key)"
        echo "  - CONTEXT_MASTER_KEY (generate with: openssl rand -hex 32)"
        echo ""

        read -p "Environment variables configured? (y/n) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo "Please configure environment variables in Railway dashboard first"
            exit 1
        fi

        echo ""
        echo "🚀 Deploying..."
        railway up

        echo ""
        echo "✅ Deployment complete!"
        echo ""
        echo "Next steps:"
        echo "  1. Run migrations: railway run psql \$DATABASE_URL -f infra/supabase/migrations/0001_init.sql"
        echo "  2. Check logs: railway logs"
        echo "  3. Open dashboard: railway open"
        ;;

    docker)
        echo "🐳 Deploying with Docker Compose..."
        echo ""

        check_tool docker
        check_tool docker-compose

        # Check for required env vars
        if [ -z "$ANTHROPIC_API_KEY" ]; then
            echo "⚠️  ANTHROPIC_API_KEY not set"
            read -p "Enter your Anthropic API key: " ANTHROPIC_API_KEY
            export ANTHROPIC_API_KEY
        fi

        echo ""
        echo "Building and starting services..."
        docker-compose up -d --build

        echo ""
        echo "Waiting for services to be healthy..."
        sleep 10

        # Check health
        if curl -s http://localhost:8000/health | grep -q "healthy"; then
            echo "✅ Gateway is healthy!"
        else
            echo "⚠️  Gateway may not be ready yet. Check logs with: docker-compose logs gateway"
        fi

        echo ""
        echo "✅ Deployment complete!"
        echo ""
        echo "Services running:"
        echo "  - Gateway: http://localhost:8000"
        echo "  - PostgreSQL: localhost:5432"
        echo "  - Redis: localhost:6379"
        echo ""
        echo "Commands:"
        echo "  - View logs: docker-compose logs -f"
        echo "  - Stop: docker-compose down"
        echo "  - Restart: docker-compose restart"
        ;;

    *)
        echo "Usage: ./deploy.sh [railway|docker]"
        echo ""
        echo "Options:"
        echo "  railway  - Deploy to Railway (recommended for production)"
        echo "  docker   - Deploy locally with Docker Compose"
        exit 1
        ;;
esac
