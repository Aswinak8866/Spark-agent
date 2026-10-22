# Spark Agent

An interactive agent that installs, configures, verifies and removes **Apache Spark 3.5.0**, either on the local machine or across remote nodes over SSH. The repo also includes an **MCP server** that lets AI clients manage a Spark cluster, and Node.js modules for **rule-engine + Groq LLM error analysis**.

## Features

### Python agent (`src/agent.py`)
- Menu-driven CLI: Verify, Install / Configure, Delete
- Startup prerequisite check (Python, curl, tar, Java); missing packages installed with apt only after confirmation
- Local mode: downloads Spark to `~/spark`, writes `spark-env.sh`, starts a Master and a Worker
- Remote mode: installs and configures a Master and Workers over SSH (Paramiko)
- Resumable download with retries
- Port checks for 7077, 8080, 8081 with a prompt for another port if busy

### MCP server (`mcp_server.js`)
Seven tools: `get_cluster_status`, `start_spark_cluster`, `stop_spark_cluster`, `run_spark_pi`, `get_saved_connections`, `get_spark_logs`, `install_spark`.

### Node.js modules (`src/steps`, `src/utils`, `src/rules`)
- Steps: prerequisites, config collection, Spark install, cluster configuration, start and verify
- Rule engine: 10 rules (R001 to R010) matching common failures and returning fix commands
- AI analyzer: rule engine first, then Groq (`llama-3.3-70b-versatile`) for a structured diagnosis
- SSH executor and saved-connection store

## Project structure

```
Spark-agent/
├── mcp_server.js
├── package.json
├── requirements.txt
├── .env.example
└── src/
    ├── agent.py
    ├── rules/rule-engine.js
    ├── steps/   (check-prerequisites, collect-config, install-spark,
    │             configure-cluster, start-verify)
    └── utils/   (ai-analyzer, connection-store, ssh-executor)
```

## Requirements

- Ubuntu / Debian Linux
- Python 3.8+ and Node.js 18+
- Internet access to download Spark
- `paramiko` for remote mode only
- Groq API key only for the AI analyzer modules

## Installation

```bash
git clone https://github.com/Aswinak8866/Spark-agent.git
cd Spark-agent
pip install -r requirements.txt
npm install
cp .env.example .env
```

## Usage

Python agent:

```bash
python3 src/agent.py
```

Choose Install / Configure, enter `0` workers for a local setup or the worker count for a remote cluster. Use Verify to check status and Delete to remove Spark.

MCP server:

```bash
npm run mcp
```

Claude Desktop config:

```json
{
  "mcpServers": {
    "spark-agent": {
      "command": "node",
      "args": ["/absolute/path/to/Spark-agent/mcp_server.js"],
      "env": { "SPARK_HOME": "/opt/spark" }
    }
  }
}
```

## Tech stack

Python, Node.js, Apache Spark 3.5.0, Java 11, Paramiko, node-ssh, MCP SDK, Groq API

## Author

Aswin Kumar S
