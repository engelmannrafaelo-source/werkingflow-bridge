module.exports = {
  apps: [
    // ========================================
    // 1. SHARED WRAPPER (eco-coach only)
    // ========================================
    {
      name: 'wrapper-shared',
      script: '/opt/homebrew/bin/poetry',
      args: 'run uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1 --timeout-keep-alive 300 --timeout-graceful-shutdown 30',
      cwd: '/Users/rafael/Documents/GitHub/eco-openai-wrapper',
      interpreter: 'none',
      env: {
        PORT: '8000',
        INSTANCE_NAME: 'shared'
        // No CLAUDE_CWD - uses wrapper root as working directory
      },
      error_file: 'logs/shared-error.log',
      out_file: 'logs/shared-out.log',
      time: true,
      kill_timeout: 60000,  // 60 seconds for graceful shutdown
      listen_timeout: 60000,  // 60 seconds for startup
      wait_ready: false,  // Don't wait for ready signal
      autorestart: true
    },

    // ========================================
    // 2. ECO-BACKEND DEDICATED WRAPPER
    // ========================================
    {
      name: 'wrapper-eco-backend',
      script: '/opt/homebrew/bin/poetry',
      args: 'run uvicorn main:app --host 0.0.0.0 --port 8010 --workers 1 --timeout-keep-alive 300 --timeout-graceful-shutdown 30',
      cwd: '/Users/rafael/Documents/GitHub/eco-openai-wrapper',
      interpreter: 'none',
      env: {
        CLAUDE_CWD: '/Users/rafael/Documents/GitHub/eco-openai-wrapper/instances/eco-backend',
        PORT: '8010',
        INSTANCE_NAME: 'eco-backend'
      },
      error_file: 'instances/eco-backend/logs/error.log',
      out_file: 'instances/eco-backend/logs/out.log',
      time: true,
      kill_timeout: 60000,  // 60 seconds for graceful shutdown
      listen_timeout: 60000,  // 60 seconds for startup
      wait_ready: false,  // Don't wait for ready signal
      autorestart: true
    },

    // ========================================
    // 3. ECO-DIAGNOSTICS DEDICATED WRAPPER
    // ========================================
    {
      name: 'wrapper-eco-diagnostics',
      script: '/opt/homebrew/bin/poetry',
      args: 'run uvicorn main:app --host 0.0.0.0 --port 8020 --workers 1 --timeout-keep-alive 300 --timeout-graceful-shutdown 30',
      cwd: '/Users/rafael/Documents/GitHub/eco-openai-wrapper',
      interpreter: 'none',
      env: {
        CLAUDE_CWD: '/Users/rafael/Documents/GitHub/eco-openai-wrapper/instances/eco-diagnostics',
        PORT: '8020',
        INSTANCE_NAME: 'eco-diagnostics'
      },
      error_file: 'instances/eco-diagnostics/logs/error.log',
      out_file: 'instances/eco-diagnostics/logs/out.log',
      time: true,
      kill_timeout: 60000,  // 60 seconds for graceful shutdown
      listen_timeout: 60000,  // 60 seconds for startup
      wait_ready: false,  // Don't wait for ready signal
      autorestart: true
    },

    // ========================================
    // 4. ECO-COACH API SERVER
    // ========================================
    {
      name: 'eco-coach-api',
      script: '/opt/homebrew/bin/python3',
      args: '-m uvicorn api.main:app --host 0.0.0.0 --port 8002',
      cwd: '/Users/rafael/Documents/GitHub/eco-coach',
      interpreter: 'none',
      env: {
        PORT: '8002'
      },
      error_file: 'logs/api-error.log',
      out_file: 'logs/api-out.log',
      time: true
    }
  ]
};