import type { EventManager } from '@arkturian/audio-guide';
import type { GuideVibe } from '@arkturian/audio-guide';
import type { IView } from '../IView';

/**
 * VibePillView — renders #vibeWrap + #vibeTag from vibe:changed events.
 * Hides the pill when vibe is null.
 */
export class VibePillView implements IView {
  private _wrap: HTMLElement | null = null;
  private _tag: HTMLElement | null = null;
  private _bus: EventManager | null = null;

  private _onVibeChanged = (payload: { vibe: GuideVibe | string | null }): void => {
    if (!payload.vibe || !this._wrap || !this._tag) {
      if (this._wrap) this._wrap.style.display = 'none';
      return;
    }
    const text = typeof payload.vibe === 'string' ? payload.vibe : payload.vibe.text;
    this._tag.textContent = text;
    this._wrap.style.display = 'block';
  };

  mount(root: HTMLElement): void {
    this._wrap = root.querySelector<HTMLElement>('#vibeWrap');
    this._tag = root.querySelector<HTMLElement>('#vibeTag');
  }

  bindEvents(bus: EventManager): void {
    this._bus = bus;
    bus.on('vibe:changed', this._onVibeChanged);
  }

  dispose(): void {
    if (this._bus) {
      this._bus.off('vibe:changed', this._onVibeChanged);
      this._bus = null;
    }
  }
}
