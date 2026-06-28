<p align="center">
  <img src="assets/banner.png" alt="Ollo R Block Assistant banner">
</p>

# Ollo R Block Assistant

A plugin for Claude Code and OpenAI Codex CLI that helps kids (roughly age 10 to 17) write and debug programs for ROBOTIS R+ Ollo robots in the R Block app.

It exists because generic AI coding assistants will happily hand a kid Python when they're working in R Block, and Python doesn't translate to the visual blocks. Students get stuck, get frustrated, and stop. This plugin teaches the AI to use the exact R Block wording instead, walk through the program one piece at a time, and explain what each block does as they place it.

## What you get

- A skill that triggers on Ollo, R Block, R+ robot, ROBOTIS, or OlloBot keywords. Works in English, Malay, and Chinese. Block code stays in English regardless of conversation language (the R Block app only renders English).
- A `/ollo` slash command for Claude Code if you want to invoke it explicitly.
- The full block reference for all categories (Control, Calculation, Variable, Function, Sensing, Motion, Screen, Sound, Line) loaded on demand.

## Install

### Claude Code

```
/plugin marketplace add GenAIMakeEasy/ollo-r-block-assistant
/plugin install ollo-r-block-assistant
```

### Codex CLI

```bash
npx codex-marketplace add GenAIMakeEasy/ollo-r-block-assistant --plugins
```

Note the plural `--plugins` flag. You'll be asked to install at project scope (just the current repo) or global scope (all projects). Pick global to keep it always on. The TUI route also works: run `codex`, then `/plugins`, switch to the marketplace tab, and install from there.

### Manual

Clone the repo, copy the skill into the right directory:

```bash
git clone https://github.com/GenAIMakeEasy/ollo-r-block-assistant.git
```

For Claude Code:
```bash
cp -r ollo-r-block-assistant/skills/ollo-r-block-assistant ~/.claude/skills/
cp ollo-r-block-assistant/commands/ollo.md ~/.claude/commands/
```

For Codex CLI, project-scoped:
```bash
mkdir -p .agents/skills
cp -r ollo-r-block-assistant/skills/ollo-r-block-assistant .agents/skills/
```

Or user-scoped:
```bash
mkdir -p ~/.codex/skills
cp -r ollo-r-block-assistant/skills/ollo-r-block-assistant ~/.codex/skills/
```

On Windows, swap `~/` for `C:\Users\<you>\`.

## How it behaves

Two modes, picked automatically from how the student opens the chat.

**Build mode.** If the student wants to make something new ("I want my robot to follow a line and stop at a crossroad"), the assistant proposes the program flow in plain language first and waits for the student to confirm before placing any blocks. Once confirmed, it builds in phases, two or three blocks at a time, and waits between phases. No full-program dumps.

**Debug mode.** If something's broken ("my robot just spins"), the assistant reads the student's blocks, finds the bug, explains the cause in one or two sentences, and shows the corrected snippet. Direct, not Socratic. Kids who are stuck don't need riddles.

Block lines come out tagged with the tab they live in, with bold for dropdown options, italic for variable names, and monospace for literal values:

> `[Motion]` Move robot's **Back axis** **Forward** at speed `50`
> `[Control]` Wait until \[ **No.1** IR sensor value **>** `300` \]

So a student can scan a line and see at a glance: which parts are pickable options, which they typed in, which are the variables they named.


**More examples**

Student: give program to let the robot t move along the square track in one round as in image shown.
<img width="939" height="673" alt="image" src="https://github.com/user-attachments/assets/42526933-fa4e-4e4f-b0df-bae374ef7f8e" />
___________________________________________________________________________________________________________________________________
<img width="1538" height="463" alt="image" src="https://github.com/user-attachments/assets/3c57ab06-fb5d-468a-a192-51df2e4081dd" />
___________________________________________________________________________________________________________________________________
<img width="1539" height="890" alt="image" src="https://github.com/user-attachments/assets/d29fd2e3-f0d6-47c3-83e9-7ae073255ba1" />
___________________________________________________________________________________________________________________________________
<img width="1534" height="1248" alt="image" src="https://github.com/user-attachments/assets/cc2819a2-8322-47af-b9b7-5b831211cd62" />
___________________________________________________________________________________________________________________________________
<img width="1583" height="879" alt="image" src="https://github.com/user-attachments/assets/b81934db-3289-4544-9a8f-70849e14b776" />

<img width="1615" height="993" alt="image" src="https://github.com/user-attachments/assets/a734982e-84f0-4e5f-80ab-96ab39a151e6" />

## Languages

English, Bahasa Malaysia, or Chinese (Simplified or Traditional). The assistant matches whatever the student writes in first, including mixed-language messages (common in Malaysia: "tolong help saya code ni"). Jargon like *sensor*, *threshold*, *variable* gets defined in both languages on first use, so kids pick up the English terms they'll see in tutorials and screenshots.

The R Block code itself never gets translated. The robot's app only knows the English block names, so translating them would just mean the student can't find the blocks.

## Contributing

PRs welcome, especially for:

- Blocks or syntax patterns I missed
- New debug heuristics
- Translations of the skill prose into other languages

## License

MIT. See [LICENSE](LICENSE).

## Acknowledgments

ROBOTIS for the Ollo R+ kits and the R Block app. Anthropic and OpenAI for the plugin systems this rides on.
