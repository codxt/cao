# Portable CAO operator agent skill

CAO ships a first-party, vendor-neutral agent skill at
[`skills/cao-operator/`](../skills/cao-operator/). It is plain instruction text
(`SKILL.md`, `agents/openai.yaml` and two references) that teaches a coding agent
how to inspect and operate CAO **within** its gates: keeping the effectful
product `./cao` CLI separate from the read-only `python -m cao` CLI, running
bounded Team Execution, treating Intake as advisory until an operator authorizes
it, and honoring the Work Registry, model-privacy, receipt, CI, merge and
deployment boundaries.

The skill ships **no executable script and no runtime dependency**. It is
documentation an agent reads.

## What the skill contains

- `SKILL.md` — the operational instructions and the fail-closed guardrails.
- `agents/openai.yaml` — display metadata (a display name and a short
  description) only.
- `references/commands.md` — the two command surfaces, exit codes and cautions.
- `references/recovery.md` — receipt verification and the bounded recovery
  paths.

## Install by symlink (portable)

The skill is not published as a package. To make it discoverable, symlink this
checkout's skill directory into the discovery directory of each agent you use.
Symlinking the directory (rather than copying) means the agent always reads the
version that is checked out here.

```sh
# Run from the CAO checkout; set repo to its absolute path if you prefer.
repo="$(pwd)"

for dir in \
  "$HOME/.agents/skills" \
  "$HOME/.codex/skills" \
  "$HOME/.config/opencode/skills"
do
  mkdir -p "$dir"
  ln -sfn "$repo/skills/cao-operator" "$dir/cao-operator"
done
```

Each agent reads a different discovery directory:

| Agent | Discovery directory |
|---|---|
| Agent skills (portable) | `~/.agents/skills` |
| Codex | `~/.codex/skills` |
| OpenCode | `~/.config/opencode/skills` |

To uninstall, remove the link from any directory above:

```sh
rm -f "$HOME/.agents/skills/cao-operator"
rm -f "$HOME/.codex/skills/cao-operator"
rm -f "$HOME/.config/opencode/skills/cao-operator"
```

## What installation does and does not do

Installing the skill only makes its **instructions discoverable** to the agent.
It grants **no authorization** and **no live CAO access**:

- It starts nothing, claims nothing and merges nothing.
- It is not an approval, a Registry claim or an operator-identity proof.
- It gives the agent no runtime, provider, credential or permission it did not
  already have.
- Every CAO gate still applies unchanged: the Work Registry decides every WRITE,
  the operator-local model policy decides routes, and publication, deployment
  and release remain separately authorized.

The skill is instruction text, so treat it as guidance an agent may follow, not
as a capability the agent gains by reading it.
