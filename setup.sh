#!/bin/bash

# Majic Movie Selector - Setup Script
# Automatically sets up virtual environment and installs dependencies

set -e

echo "🎬 Majic Movie Selector Setup"
echo "=============================="

# Check if Python 3.11+ is available
python_cmd=""
if command -v python3.12 &> /dev/null; then
    python_cmd="python3.12"
elif command -v python3.11 &> /dev/null; then
    python_cmd="python3.11"
elif command -v python3 &> /dev/null; then
    python_version=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    if [[ $(echo "$python_version >= 3.11" | bc -l) -eq 1 ]]; then
        python_cmd="python3"
    else
        echo "❌ Python 3.11+ required. Found: $python_version"
        exit 1
    fi
else
    echo "❌ Python 3 not found. Please install Python 3.11+"
    exit 1
fi

echo "✅ Using Python: $($python_cmd --version)"

# Remove existing venv if it exists
if [ -d ".venv" ]; then
    echo "🗑️  Removing existing virtual environment..."
    rm -rf .venv
fi

# Create new virtual environment
echo "📦 Creating virtual environment..."
$python_cmd -m venv .venv

# Activate virtual environment and install dependencies
echo "⬇️  Installing dependencies..."
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Install additional dependencies for desktop app and MCP
echo "🔧 Installing additional dependencies..."
pip install mcp pywebview

# Make majic script executable
echo "🔑 Making majic script executable..."
chmod +x majic

# Create .env file if it doesn't exist
if [ ! -f ".env" ]; then
    echo "📝 Creating .env file from template..."
    cp .env.example .env
    echo "⚠️  Please edit .env file with your API keys and settings"
fi

echo ""
echo "🎉 Setup complete!"
echo ""
echo "Next steps:"
echo "1. Edit .env file with your configuration"
echo "2. Pull the recommended model: ollama pull llama3.3:70b-instruct-q4_K_M"
echo "3. Start the app: ./majic dev"
echo ""
echo "Available commands:"
echo "  ./majic dev     - Development mode"
echo "  ./majic start   - Production mode"
echo "  ./majic status  - Check status"
echo "  ./majic logs    - View logs"
echo ""
