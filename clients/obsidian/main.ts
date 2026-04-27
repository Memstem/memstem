/*
 * Memstem Obsidian plugin — v0.1 scaffold.
 *
 * For v0.1 the plugin only proves the integration loop end-to-end:
 *   - On load, it reaches out to the Memstem daemon's /health endpoint.
 *   - It shows the daemon connection state in the status bar.
 *   - A settings tab lets the user point at a non-default daemon URL.
 *
 * Real features (search modal, sidebar pane, "New memory" command,
 * frontmatter scaffolding, Dataview-style code blocks) follow in
 * subsequent PRs. Keeping the surface area small here makes the
 * scaffold obvious and reviewable.
 */

import { App, Plugin, PluginSettingTab, Setting } from "obsidian";

interface MemstemSettings {
  daemonUrl: string;
  pollIntervalMs: number;
}

const DEFAULT_SETTINGS: MemstemSettings = {
  daemonUrl: "http://127.0.0.1:7821",
  pollIntervalMs: 30_000,
};

interface HealthResponse {
  status: string;
  version: string;
  vault: string;
  embedder: boolean;
}

export default class MemstemPlugin extends Plugin {
  settings!: MemstemSettings;
  private statusBarEl?: HTMLElement;
  private healthTimer?: number;

  async onload(): Promise<void> {
    await this.loadSettings();

    this.statusBarEl = this.addStatusBarItem();
    this.statusBarEl.setText("Memstem: connecting…");

    this.addSettingTab(new MemstemSettingTab(this.app, this));

    this.addCommand({
      id: "memstem-check-daemon",
      name: "Memstem: Check daemon connection",
      callback: () => {
        void this.refreshHealth(true);
      },
    });

    // Initial check + recurring poll. Recurring poll catches daemon
    // restarts so the status bar stops lying after a `pm2 restart memstem`.
    void this.refreshHealth();
    this.healthTimer = window.setInterval(
      () => void this.refreshHealth(),
      this.settings.pollIntervalMs,
    );
    this.registerInterval(this.healthTimer);
  }

  async loadSettings(): Promise<void> {
    this.settings = { ...DEFAULT_SETTINGS, ...(await this.loadData()) };
  }

  async saveSettings(): Promise<void> {
    await this.saveData(this.settings);
  }

  /**
   * Fetch /health from the configured daemon URL and update the status bar.
   * If `notify` is true, also surfaces the result in a toast — used by the
   * "Check daemon connection" command for explicit user-driven probes.
   */
  async refreshHealth(notify = false): Promise<void> {
    if (!this.statusBarEl) return;
    try {
      const url = new URL("/health", this.settings.daemonUrl).toString();
      const res = await fetch(url, { method: "GET" });
      if (!res.ok) {
        throw new Error(`daemon returned ${res.status}`);
      }
      const body = (await res.json()) as HealthResponse;
      const embedderTag = body.embedder ? "embed:on" : "embed:off";
      this.statusBarEl.setText(`Memstem ${body.version} ✓ ${embedderTag}`);
      this.statusBarEl.title = `Vault: ${body.vault}\nDaemon: ${this.settings.daemonUrl}`;
      if (notify) {
        // eslint-disable-next-line no-new
        new (window as unknown as { Notice: new (msg: string) => unknown }).Notice(
          `Memstem ${body.version} is up at ${this.settings.daemonUrl}`,
        );
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      this.statusBarEl.setText("Memstem ✗ offline");
      this.statusBarEl.title = `Could not reach ${this.settings.daemonUrl}\n${message}`;
      if (notify) {
        // eslint-disable-next-line no-new
        new (window as unknown as { Notice: new (msg: string) => unknown }).Notice(
          `Memstem daemon unreachable: ${message}`,
        );
      }
    }
  }

  onunload(): void {
    // Obsidian auto-clears registered intervals; nothing else to do.
  }
}

class MemstemSettingTab extends PluginSettingTab {
  constructor(
    app: App,
    private readonly plugin: MemstemPlugin,
  ) {
    super(app, plugin);
  }

  display(): void {
    const { containerEl } = this;
    containerEl.empty();

    containerEl.createEl("h2", { text: "Memstem" });
    containerEl.createEl("p", {
      text: "Connection settings for the local Memstem daemon. Defaults match a stock `memstem daemon` install.",
    });

    new Setting(containerEl)
      .setName("Daemon URL")
      .setDesc("Loopback HTTP server hosted by `memstem daemon`. Default: http://127.0.0.1:7821.")
      .addText((text) =>
        text
          .setPlaceholder("http://127.0.0.1:7821")
          .setValue(this.plugin.settings.daemonUrl)
          .onChange(async (value) => {
            this.plugin.settings.daemonUrl = value.trim() || DEFAULT_SETTINGS.daemonUrl;
            await this.plugin.saveSettings();
            void this.plugin.refreshHealth();
          }),
      );

    new Setting(containerEl)
      .setName("Health-check interval (ms)")
      .setDesc("How often the status bar pings the daemon. 0 disables polling.")
      .addText((text) =>
        text
          .setValue(String(this.plugin.settings.pollIntervalMs))
          .onChange(async (value) => {
            const parsed = Number.parseInt(value, 10);
            if (Number.isFinite(parsed) && parsed >= 0) {
              this.plugin.settings.pollIntervalMs = parsed;
              await this.plugin.saveSettings();
            }
          }),
      );
  }
}
