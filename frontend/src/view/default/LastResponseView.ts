import type { EventManager } from '@arkturian/audio-guide';
import type { IView } from '../IView';

/**
 * LastResponseView — shows Guide's latest textual response in
 * #lastResponse / #responseText. Hidden by default.
 */
export class LastResponseView implements IView {
  private _wrap: HTMLElement | null = null;
  private _text: HTMLElement | null = null;
  private _bus: EventManager | null = null;

  private _onResponseText = (payload: { text: string }): void => {
    if (!this._wrap || !this._text) return;
    this._text.textContent = payload.text;
    this._wrap.style.display = payload.text ? 'block' : 'none';
  };

  mount(root: HTMLElement): void {
    this._wrap = root.querySelector<HTMLElement>('#lastResponse');
    this._text = root.querySelector<HTMLElement>('#responseText');
  }

  bindEvents(bus: EventManager): void {
    this._bus = bus;
    bus.on('response:text', this._onResponseText);
  }

  dispose(): void {
    if (this._bus) {
      this._bus.off('response:text', this._onResponseText);
      this._bus = null;
    }
  }
}
