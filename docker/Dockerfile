FROM python:3.12-slim

# Install system deps for Node.js, gosu, and general utils
RUN apt-get update && apt-get install -y \
    curl \
    nodejs \
    npm \
    gosu \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for Claude Code (required for bypassPermissions)
RUN useradd -m -u 1001 -s /bin/bash claude

# Install Poetry globally as root, but make it accessible system-wide
RUN curl -sSL https://install.python-poetry.org | POETRY_HOME=/usr/local python3 -

# Add Poetry to PATH
ENV PATH="/usr/local/bin:${PATH}"

# Install Claude Code CLI globally (for SDK compatibility)
RUN npm install -g @anthropic-ai/claude-code

# Copy the app code
COPY . /app

# Set working directory
WORKDIR /app

# Copy SuperClaude Framework plugins and commands for claude user (if available)
RUN mkdir -p /home/claude/.claude/plugins /home/claude/.claude/commands

# Copy container-specific MCP config for claude user
COPY config/docker-claude.json /home/claude/.claude.json

# Create directories for volumes
RUN mkdir -p /app/logs /app/instances

# Install Python dependencies with Poetry (as root, creates virtualenv in /app/.venv)
# Including privacy extras for DSGVO-compliant PII anonymization
# poetry.lock is in .gitignore so we must regenerate it from pyproject.toml
RUN poetry config virtualenvs.in-project true && poetry lock && poetry install --no-root --extras "privacy"

# Download spaCy language models for Presidio PII detection
RUN poetry run python -m spacy download de_core_news_lg && \
    poetry run python -m spacy download en_core_web_lg

# Set ownership of entire app directory and claude home to claude user
RUN chown -R claude:claude /app /home/claude

# Copy and set up entrypoint script
COPY docker/docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Container starts as root to fix volume permissions, then drops to claude user
ENTRYPOINT ["docker-entrypoint.sh"]

# Expose the port (default 8000)
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

# Run the app with Uvicorn (production mode without reload)
CMD ["poetry", "run", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]