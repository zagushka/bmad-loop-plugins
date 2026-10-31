# bmad-loop plugins

Add notifications or GitHub delivery to your bmad-loop project. Choose the plugin you need, or use both.

## Choose a plugin

| Plugin | What it does | Version |
| --- | --- | --- |
| [status-notify](docs/status-notify/README.md) | Sends run and story updates, pauses, permission waits and lifecycle events through a command you choose. | 1.1.0 |
| [delivery](docs/delivery/README.md) | Opens GitHub pull requests for completed work, checks review and acceptance evidence, and records Ticket Tree completion after the required merges. | 0.2.0 |

Use **status-notify** for progress messages. You supply the sender command and destination. Messages can be lost or arrive out of order; they do not control the run.

Use **delivery** to deliver completed tickets. It needs a supported Ticket Tree project, GitHub access and acceptance evidence from an authorized orchestrator. Local completion alone does not approve delivery.

## Install

You need **Python 3.11+**, **Git** and **bmad-loop 0.13.2 / plugin API 1** in the target project. The manager uses Python's standard library; there is nothing to install with pip.

- **status-notify:** macOS or Linux, plus a separately installed sender command.
- **delivery:** tested on macOS with Node 22, pnpm 10.17.1, `gh` and a native repo-local BMad Ticket Tree project.

Stop the target project's loop and workers before installation or updates.

### 1. Get this collection

```sh
git clone https://github.com/zagushka/bmad-loop-plugins.git
cd bmad-loop-plugins
```

### 2. Install your plugin

Replace `/absolute/project` with the full path to your bmad-loop project. Run the command for the plugin you chose, or run both:

```sh
python3 -B manage.py install status-notify --version 1.1.0 --project /absolute/project
```

```sh
python3 -B manage.py install delivery --version 0.2.0 --project /absolute/project
```

The manager installs the selected release into your project's `.bmad-loop/plugins/` folder. It records the version in `.bmad-loop/plugins.lock.json`. It does not start a loop or worker.

### 3. Configure and enable

Follow the setup guide for your plugin:

- **[Set up status-notify](docs/status-notify/README.md#configure-a-new-run):** add a sender command and destination to `.bmad-loop/policy.toml`.
- **[Set up delivery](docs/delivery/README.md#install-and-grant):** create a private configuration file, set its path in the policy, and keep `tickets.mark_done = "never"`.

Review the installed code and settings. Then run the exact `plugins enable` command printed by the manager. This is a separate approval step; installation does not grant code permission to run.

Check the installation:

```sh
python3 -B manage.py status --project /absolute/project
```

After the enable command succeeds, start your next run through your normal bmad-loop workflow.

## Update

With the loop and workers stopped, fetch release tags and select a released version explicitly:

```sh
git fetch origin --tags
python3 -B manage.py update status-notify --version 1.1.0 --project /absolute/project
```

Replace the plugin name and version with the release you want. Review changed code and settings, then run the printed `plugins accept` command. The manager does not overwrite local plugin edits or approve changed code automatically.

## More information

- [Plugin manager reference](docs/plugin-manager.md): release checks, lock files, rollback and recovery after an interrupted installation.
- [Delivery configuration](docs/delivery/configuration.md) and [integration guide](docs/delivery/integration.md).
- [Contributing and releases](CONTRIBUTING.md).

Run the test suite from this collection's directory:

```sh
python3 -B -m unittest discover -s tests -v
```
