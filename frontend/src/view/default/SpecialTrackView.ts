import type { EventManager } from '@arkturian/audio-guide';
import type { SpecialTrackOption } from '@arkturian/audio-guide';
import type { IView } from '../IView';

/**
 * SpecialTrackView — dropdown that lets the user explicitly enable
 * a curated "special geo" track (e.g. Tscheppaschlucht + Dr. Tschauko
 * persona) or stay in the generic Guide mode.
 *
 * Binds to #specialTrackSelect. Emits user:special-track-change with
 * the chosen track_id (or null for off). Reacts to special-track:changed
 * bus events to keep the dropdown in sync with the model (e.g. after
 * a reload when the persisted value is loaded).
 */
export class SpecialTrackView implements IView {
  private _select: HTMLSelectElement | null = null;
  private _bus: EventManager | null = null;
  private _changeHandler: EventListener | null = null;

  private _onModelChanged = (payload: { current: SpecialTrackOption }): void => {
    if (!this._select) return;
    const id = payload.current.id;
    this._select.value = id === null ? '0' : String(id);
  };

  mount(root: HTMLElement): void {
    this._select = root.querySelector<HTMLSelectElement>('#specialTrackSelect');
  }

  bindEvents(bus: EventManager): void {
    this._bus = bus;
    if (this._select) {
      this._changeHandler = () => {
        const raw = this._select!.value;
        const parsed = parseInt(raw, 10);
        const trackId = parsed === 0 || !Number.isFinite(parsed) ? null : parsed;
        bus.emit('user:special-track-change', { trackId });
      };
      this._select.addEventListener('change', this._changeHandler);
    }
    bus.on('special-track:changed', this._onModelChanged);
  }

  dispose(): void {
    if (this._select && this._changeHandler) {
      this._select.removeEventListener('change', this._changeHandler);
      this._changeHandler = null;
    }
    if (this._bus) {
      this._bus.off('special-track:changed', this._onModelChanged);
      this._bus = null;
    }
  }
}
