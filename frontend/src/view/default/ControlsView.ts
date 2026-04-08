import type { EventManager } from '@arkturian/audio-guide';
import type { RideMode } from '@arkturian/audio-guide';
import type { TrackingSnapshot } from '@arkturian/audio-guide';
import type { IView } from '../IView';

/**
 * ControlsView — binds all the action buttons (#startBtn, #stopBtn,
 * #driftBtn, #micBtn, #moreBtn, #pauseBtn) + the mode selector
 * (.mode-btn[data-mode]). Emits user-intent events on the bus; the
 * controller listens and orchestrates the actual behaviour.
 */
export class ControlsView implements IView {
  private _startBtn: HTMLButtonElement | null = null;
  private _stopBtn: HTMLButtonElement | null = null;
  private _driftBtn: HTMLButtonElement | null = null;
  private _micBtn: HTMLButtonElement | null = null;
  private _moreBtn: HTMLButtonElement | null = null;
  private _pauseBtn: HTMLButtonElement | null = null;
  private _rebootBtn: HTMLButtonElement | null = null;
  private _modeBtns: HTMLButtonElement[] = [];
  private _bus: EventManager | null = null;

  private _handlers: { el: HTMLElement; ev: string; fn: EventListener }[] = [];

  private _onTrackingChanged = (snap: TrackingSnapshot): void => {
    if (this._startBtn && this._stopBtn) {
      this._startBtn.style.display = snap.isRunning ? 'none' : 'block';
      this._stopBtn.style.display = snap.isRunning ? 'block' : 'none';
    }
    // Mode button active state
    for (const btn of this._modeBtns) {
      const mode = btn.dataset.mode as RideMode | undefined;
      if (mode === snap.currentMode) {
        btn.classList.add('active');
      } else {
        btn.classList.remove('active');
      }
    }
  };

  mount(root: HTMLElement): void {
    this._startBtn = root.querySelector<HTMLButtonElement>('#startBtn');
    this._stopBtn = root.querySelector<HTMLButtonElement>('#stopBtn');
    this._driftBtn = root.querySelector<HTMLButtonElement>('#driftBtn');
    this._micBtn = root.querySelector<HTMLButtonElement>('#micBtn');
    this._moreBtn = root.querySelector<HTMLButtonElement>('#moreBtn');
    this._pauseBtn = root.querySelector<HTMLButtonElement>('#pauseBtn');
    this._rebootBtn = root.querySelector<HTMLButtonElement>('#rebootBtn');
    this._modeBtns = Array.from(root.querySelectorAll<HTMLButtonElement>('.mode-btn'));
  }

  bindEvents(bus: EventManager): void {
    this._bus = bus;

    const bind = (el: HTMLElement | null, ev: string, fn: EventListener) => {
      if (!el) return;
      el.addEventListener(ev, fn);
      this._handlers.push({ el, ev, fn });
    };

    bind(this._startBtn, 'click', () => bus.emit('user:start'));
    bind(this._stopBtn, 'click', () => bus.emit('user:stop'));
    bind(this._driftBtn, 'click', () => bus.emit('user:drift-request'));
    bind(this._micBtn, 'click', () => bus.emit('user:mic-toggle'));
    bind(this._moreBtn, 'click', () => bus.emit('user:more-like-this'));
    bind(this._pauseBtn, 'click', () => bus.emit('user:tts-pause'));
    bind(this._rebootBtn, 'click', () => bus.emit('user:reboot-guide'));

    for (const btn of this._modeBtns) {
      const mode = btn.dataset.mode as RideMode | undefined;
      if (!mode) continue;
      bind(btn, 'click', () => bus.emit('user:mode-change', { mode }));
    }

    bus.on('tracking:changed', this._onTrackingChanged);
  }

  dispose(): void {
    for (const h of this._handlers) {
      h.el.removeEventListener(h.ev, h.fn);
    }
    this._handlers = [];
    if (this._bus) {
      this._bus.off('tracking:changed', this._onTrackingChanged);
      this._bus = null;
    }
  }
}
