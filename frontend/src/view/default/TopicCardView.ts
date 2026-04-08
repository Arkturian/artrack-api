import type { EventManager } from '@arkturian/audio-guide';
import type { GuideTopic } from '@arkturian/audio-guide';
import type { IView } from '../IView';

interface TopicChangedPayload {
  topic: GuideTopic | null;
  isEnriching: boolean;
}

/**
 * TopicCardView — renders #topicCard from topic:changed events.
 * Shows image with shimmer loader, title, subtitle, extract, tags,
 * wiki/maps action buttons. Hides the card when topic is null.
 */
export class TopicCardView implements IView {
  private _card: HTMLElement | null = null;
  private _imgWrap: HTMLElement | null = null;
  private _img: HTMLImageElement | null = null;
  private _title: HTMLElement | null = null;
  private _subtitle: HTMLElement | null = null;
  private _extract: HTMLElement | null = null;
  private _tags: HTMLElement | null = null;
  private _actions: HTMLElement | null = null;
  private _bus: EventManager | null = null;

  private _onTopicChanged = (payload: TopicChangedPayload): void => {
    const { topic, isEnriching } = payload;
    if (!topic) {
      this._hide();
      return;
    }
    this._render(topic, isEnriching);
  };

  private _render(topic: GuideTopic, isEnriching: boolean): void {
    if (!this._card) return;
    this._card.style.display = 'block';

    if (this._title) this._title.textContent = topic.title || '';
    if (this._subtitle) this._subtitle.textContent = topic.subtitle || '';

    if (this._extract) {
      if (topic.description) {
        this._extract.textContent = topic.description;
        this._extract.style.display = 'block';
      } else {
        this._extract.textContent = '';
        this._extract.style.display = 'none';
      }
    }

    // Image handling — show shimmer while loading
    if (this._imgWrap && this._img) {
      if (topic.image) {
        this._imgWrap.style.display = 'flex';
        this._imgWrap.classList.add('topic-image-loading');
        this._img.onload = () => this._imgWrap?.classList.remove('topic-image-loading');
        this._img.onerror = () => this._imgWrap?.classList.remove('topic-image-loading');
        this._img.src = topic.image;
        this._img.alt = topic.title || '';
      } else if (isEnriching) {
        // Enrichment in progress — show shimmer placeholder
        this._imgWrap.style.display = 'flex';
        this._imgWrap.classList.add('topic-image-loading');
        this._img.src = '';
        this._img.alt = '';
      } else {
        this._imgWrap.style.display = 'none';
        this._imgWrap.classList.remove('topic-image-loading');
      }
    }

    // Tags
    if (this._tags) {
      this._tags.innerHTML = '';
      for (const tag of topic.tags || []) {
        const el = document.createElement('span');
        el.className = 'topic-tag';
        el.textContent = tag;
        this._tags.appendChild(el);
      }
    }

    // Action buttons (wikipedia / maps)
    if (this._actions) {
      this._actions.innerHTML = '';
      if (topic.wikipedia) {
        const a = document.createElement('a');
        a.className = 'topic-btn topic-btn-wiki';
        a.href = topic.wikipedia;
        a.target = '_blank';
        a.rel = 'noopener';
        a.textContent = '📖 Wikipedia';
        this._actions.appendChild(a);
      }
      if (topic.maps) {
        const a = document.createElement('a');
        a.className = 'topic-btn topic-btn-maps';
        a.href = topic.maps;
        a.target = '_blank';
        a.rel = 'noopener';
        a.textContent = '🗺 Maps';
        this._actions.appendChild(a);
      }
    }
  }

  private _hide(): void {
    if (this._card) this._card.style.display = 'none';
  }

  mount(root: HTMLElement): void {
    this._card = root.querySelector<HTMLElement>('#topicCard');
    this._imgWrap = root.querySelector<HTMLElement>('#topicImgWrap');
    this._img = root.querySelector<HTMLImageElement>('#topicImg');
    this._title = root.querySelector<HTMLElement>('#topicTitle');
    this._subtitle = root.querySelector<HTMLElement>('#topicSubtitle');
    this._extract = root.querySelector<HTMLElement>('#topicExtract');
    this._tags = root.querySelector<HTMLElement>('#topicTags');
    this._actions = root.querySelector<HTMLElement>('#topicActions');
  }

  bindEvents(bus: EventManager): void {
    this._bus = bus;
    bus.on('topic:changed', this._onTopicChanged);
  }

  dispose(): void {
    if (this._bus) {
      this._bus.off('topic:changed', this._onTopicChanged);
      this._bus = null;
    }
  }
}
