import type { EventManager } from '@arkturian/audio-guide';
import type { BudgetSnapshot } from '@arkturian/audio-guide';
import type { IView } from '../IView';

/**
 * BudgetView — binds #rideBudget input + #intervalSlider, renders
 * #budgetRemain + #intervalLabel. Emits user:budget-change and
 * user:interval-change for the controller.
 */
export class BudgetView implements IView {
  private _budgetInput: HTMLInputElement | null = null;
  private _remainDisplay: HTMLElement | null = null;
  private _intervalSlider: HTMLInputElement | null = null;
  private _intervalLabel: HTMLElement | null = null;
  private _bus: EventManager | null = null;
  private _handlers: { el: HTMLElement; ev: string; fn: EventListener }[] = [];

  private _onBudgetTick = (snap: BudgetSnapshot): void => {
    if (!this._remainDisplay) return;
    if (snap.isUnlimited) {
      this._remainDisplay.textContent = '∞';
      this._remainDisplay.style.color = '#7aa2f7';
      return;
    }
    const rem = snap.remainingMin;
    this._remainDisplay.textContent = `${rem} min`;
    if (rem < 10) this._remainDisplay.style.color = '#f7768e';
    else if (rem < 20) this._remainDisplay.style.color = '#e0af68';
    else this._remainDisplay.style.color = '#9ece6a';
  };

  mount(root: HTMLElement): void {
    this._budgetInput = root.querySelector<HTMLInputElement>('#rideBudget');
    this._remainDisplay = root.querySelector<HTMLElement>('#budgetRemain');
    this._intervalSlider = root.querySelector<HTMLInputElement>('#intervalSlider');
    this._intervalLabel = root.querySelector<HTMLElement>('#intervalLabel');
  }

  bindEvents(bus: EventManager): void {
    this._bus = bus;

    const bind = (el: HTMLElement | null, ev: string, fn: EventListener) => {
      if (!el) return;
      el.addEventListener(ev, fn);
      this._handlers.push({ el, ev, fn });
    };

    bind(this._budgetInput, 'input', () => {
      const val = parseInt(this._budgetInput?.value || '0', 10) || 0;
      bus.emit('user:budget-change', { minutes: val });
    });

    bind(this._intervalSlider, 'input', () => {
      const val = parseInt(this._intervalSlider?.value || '60', 10) || 60;
      if (this._intervalLabel) this._intervalLabel.textContent = `${val}s`;
      bus.emit('user:interval-change', { seconds: val });
    });

    bus.on('budget:tick', this._onBudgetTick);
    bus.on('budget:changed', this._onBudgetTick);
  }

  dispose(): void {
    for (const h of this._handlers) {
      h.el.removeEventListener(h.ev, h.fn);
    }
    this._handlers = [];
    if (this._bus) {
      this._bus.off('budget:tick', this._onBudgetTick);
      this._bus.off('budget:changed', this._onBudgetTick);
      this._bus = null;
    }
  }
}
