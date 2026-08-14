# Managed Python Agent

Edit `src/main.py` to implement your repair strategy. The Agent reads task
metadata and the initial failure log from `/workspace/input`, modifies only
`/workspace/work/repo`, and writes `/workspace/output/agent-result.json`.

Run:

```bash
./bb check --agent ./agents/AGENT_NAME
./bb test --agent ./agents/AGENT_NAME
./bb package --agent ./agents/AGENT_NAME
```

