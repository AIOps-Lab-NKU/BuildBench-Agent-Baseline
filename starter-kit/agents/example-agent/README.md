# Example Agent

This trusted demonstration Agent reads the provided initial build log and
repairs one explicit marker in the writable hello package spec. It is designed
to demonstrate the submission entrypoint and workspace contract, not repair
quality.

The Agent:

1. reads `/workspace/input/initial-build.log`;
2. edits only `/workspace/work/repo/input/buildbench-hello.spec`;
3. writes `/workspace/output/agent-result.json`.

