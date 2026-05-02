#!/bin/bash

# Smart Ollama Model Selector
# Automatically detects system memory and downloads the optimal model

set -e

echo "🧠 Smart Ollama Model Selector"
echo "=============================="

# Function to get available memory in GB
get_memory_gb() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS
        local mem_bytes=$(sysctl -n hw.memsize)
        echo $(($mem_bytes / 1024 / 1024 / 1024))
    else
        # Linux
        local mem_kb=$(grep MemTotal /proc/meminfo | awk '{print $2}')
        echo $(($mem_kb / 1024 / 1024))
    fi
}

# Function to get available RAM (free + available)
get_available_memory_gb() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS - use vm_stat
        local page_size=$(vm_stat | head -1 | sed 's/.*page size of \([0-9]*\).*/\1/')
        local free_pages=$(vm_stat | grep "Pages free" | awk '{print $3}' | sed 's/\.//')
        local inactive_pages=$(vm_stat | grep "Pages inactive" | awk '{print $3}' | sed 's/\.//')
        local available_bytes=$((($free_pages + $inactive_pages) * $page_size))
        echo $(($available_bytes / 1024 / 1024 / 1024))
    else
        # Linux
        local mem_info=$(grep -E "MemAvailable|MemFree" /proc/meminfo | awk '{print $2}')
        local available_kb=$(echo $mem_info | awk '{sum+=$1} END {print sum}')
        echo $(($available_kb / 1024 / 1024))
    fi
}

# Function to check if model exists
model_exists() {
    ollama list | grep -q "^$1\b" 2>/dev/null
}

# Function to download model with progress
download_model() {
    local model=$1
    echo "📥 Downloading $model..."
    
    if ! ollama pull "$model"; then
        echo "❌ Failed to download $model"
        return 1
    fi
    
    echo "✅ Successfully downloaded $model"
    return 0
}

# Function to update .env file
update_env_model() {
    local model=$1
    local env_file=".env"
    
    if [[ -f "$env_file" ]]; then
        if grep -q "OLLAMA_MODEL=" "$env_file"; then
            sed -i.bak "s/OLLAMA_MODEL=.*/OLLAMA_MODEL=$model/" "$env_file"
            echo "📝 Updated .env with OLLAMA_MODEL=$model"
        else
            echo "OLLAMA_MODEL=$model" >> "$env_file"
            echo "📝 Added OLLAMA_MODEL=$model to .env"
        fi
    else
        echo "⚠️  .env file not found. Creating it..."
        echo "OLLAMA_MODEL=$model" > "$env_file"
    fi
}

# Main logic
main() {
    # Check if ollama is running
    if ! ollama list &>/dev/null; then
        echo "🚀 Starting Ollama server..."
        ollama serve &
        sleep 3
    fi

    # Get system memory info
    total_memory=$(get_memory_gb)
    available_memory=$(get_available_memory_gb)
    
    echo "💾 System Memory: ${total_memory}GB total, ${available_memory}GB available"
    
    # Model selection based on available memory
    selected_model=""
    model_description=""
    
    if [[ $available_memory -ge 48 ]]; then
        selected_model="llama3.3:70b-instruct-q4_K_M"
        model_description="Llama 3.3 70B (GPT-4 class, best performance)"
    elif [[ $available_memory -ge 32 ]]; then
        selected_model="qwen2.5:32b"
        model_description="Qwen2.5 32B (Excellent multilingual support)"
    elif [[ $available_memory -ge 16 ]]; then
        selected_model="deepseek-r1:14b"
        model_description="DeepSeek R1 14B (Chain-of-thought reasoning)"
    elif [[ $available_memory -ge 8 ]]; then
        selected_model="llama3.1:8b-instruct-q8_0"
        model_description="Llama 3.1 8B (Good balance of speed/capability)"
    elif [[ $available_memory -ge 4 ]]; then
        selected_model="qwen2.5:7b"
        model_description="Qwen2.5 7B (Efficient for general tasks)"
    else
        selected_model="phi3:mini"
        model_description="Phi-3 Mini (Ultra-lightweight, <4GB RAM)"
    fi
    
    echo ""
    echo "🎯 Recommended Model: $selected_model"
    echo "📋 $model_description"
    echo ""
    
    # Check if model already exists
    if model_exists "$selected_model"; then
        echo "✅ Model '$selected_model' already exists locally"
        
        # Ask if user wants to update .env
        read -p "📝 Update .env to use this model? (y/N): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            update_env_model "$selected_model"
        fi
        
        echo "🚀 Ready to use! Start with: ./majic dev"
        return 0
    fi
    
    # Show model size estimate
    echo "📊 Estimated download size: ~$(echo $selected_model | sed 's/.*://' | sed 's/_.*//' | sed 's/b/GB/')"
    echo ""
    
    # Ask for confirmation
    read -p "📥 Download '$selected_model'? (Y/n): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Nn]$ ]]; then
        echo "❌ Download cancelled"
        return 1
    fi
    
    # Download the model
    if download_model "$selected_model"; then
        # Update .env file
        update_env_model "$selected_model"
        
        echo ""
        echo "🎉 Setup complete!"
        echo "🚀 Start the app with: ./majic dev"
        echo "🌐 Access at: http://$(hostname -I | awk '{print $1}'):8080"
    else
        echo "❌ Failed to download model"
        return 1
    fi
}

# Additional function to list available models
list_models() {
    echo "📋 Available models by memory requirement:"
    echo ""
    echo "48GB+ RAM:  llama3.3:70b-instruct-q4_K_M  (GPT-4 class)"
    echo "32GB+ RAM:  qwen2.5:32b                      (Multilingual)"
    echo "16GB+ RAM:  deepseek-r1:14b                  (Reasoning)"
    echo "8GB+ RAM:   llama3.1:8b-instruct-q8_0        (Balanced)"
    echo "4GB+ RAM:   qwen2.5:7b                       (Efficient)"
    echo "<4GB RAM:   phi3:mini                        (Ultra-light)"
}

# CLI argument parsing
case "${1:-}" in
    "list"|"--list"|"-l")
        list_models
        ;;
    "help"|"--help"|"-h")
        echo "Usage: $0 [list|help]"
        echo "  list  - Show available models by memory requirement"
        echo "  help  - Show this help message"
        echo ""
        echo "Default: Automatically detect memory and download optimal model"
        ;;
    "")
        main
        ;;
    *)
        echo "Unknown option: $1"
        echo "Use '$0 help' for usage information"
        exit 1
        ;;
esac
