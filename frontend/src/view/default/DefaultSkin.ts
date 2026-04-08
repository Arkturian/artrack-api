import type { EventManager } from '@arkturian/audio-guide';
import type { IView } from '../IView';

import templateHtml from './template.html?raw';
import './styles.css';

import { StatusPanelView } from './StatusPanelView';
import { TopicCardView } from './TopicCardView';
import { VibePillView } from './VibePillView';
import { ControlsView } from './ControlsView';
import { BudgetView } from './BudgetView';
import { YoutubePanelView } from './YoutubePanelView';
import { LastResponseView } from './LastResponseView';
import { LogView } from './LogView';
import { SpecialTrackView } from './SpecialTrackView';

/**
 * DefaultSkin — the initial (Tokyo-Night) view skin. Orchestrates
 * template injection + child view instantiation + event binding.
 *
 * To build a second skin: copy this folder to `view/myskin/`, write
 * a new template.html + styles.css + view classes following the
 * same IView interface, then in main.ts choose which skin to use.
 */
export class DefaultSkin {
  private _views: IView[] = [];

  constructor(private _sessionName: string) {}

  /** Inject the HTML fragment into #app and instantiate all child views */
  mount(root: HTMLElement): void {
    root.innerHTML = templateHtml;

    // Update the page title with session name
    const title = root.querySelector<HTMLElement>('#appTitle');
    if (title) title.textContent = `GPS Tracker → ${this._sessionName}`;

    // Instantiate and mount all child views
    this._views = [
      new StatusPanelView(),
      new TopicCardView(),
      new VibePillView(),
      new ControlsView(),
      new BudgetView(),
      new SpecialTrackView(),
      new YoutubePanelView(),
      new LastResponseView(),
      new LogView(),
    ];

    for (const view of this._views) {
      view.mount(root);
    }
  }

  /** Bind all child views to the event bus */
  bindEvents(bus: EventManager): void {
    for (const view of this._views) {
      view.bindEvents(bus);
    }
  }

  dispose(): void {
    for (const view of this._views) {
      view.dispose();
    }
    this._views = [];
  }
}
