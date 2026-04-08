import type { EventManager, LogEntry } from '@arkturian/audio-guide';
import type { IView } from '../IView';

/**
 * LogView — appends to #log on every log:message event. Auto-scrolls
 * to keep the latest line visible. Caps the number of entries to
 * avoid unbounded memory growth.
 */
export class LogView implements IView {
  private static readonly MAX_ENTRIES = 200;
  private _log: HTMLElement | null = null;
  private _bus: EventManager | null = null;

  private _onLogMessage = (entry: LogEntry): void => {
    if (!this._log) return;
    const ts = new Date(entry.timestamp).toLocaleTimeString('de-DE');
    const line = document.createElement('div');
    line.textContent = `[${ts}] ${entry.text}`;
    switch (entry.level) {
      case 'error':
        line.style.color = '#f7768e';
        break;
      case 'warn':
        line.style.color = '#e0af68';
        break;
      case 'debug':
        line.style.color = '#565f89';
        break;
      default:
        line.style.color = '#a9b1d6';
    }
    this._log.appendChild(line);
    // Cap entries
    while (this._log.childElementCount > LogView.MAX_ENTRIES) {
      this._log.firstElementChild?.remove();
    }
    // Auto-scroll
    this._log.scrollTop = this._log.scrollHeight;
  };

  mount(root: HTMLElement): void {
    this._log = root.querySelector<HTMLElement>('#log');
  }

  bindEvents(bus: EventManager): void {
    this._bus = bus;
    bus.on('log:message', this._onLogMessage);
  }

  dispose(): void {
    if (this._bus) {
      this._bus.off('log:message', this._onLogMessage);
      this._bus = null;
    }
  }
}
