import type { EventManager } from '@arkturian/audio-guide';
import type { SongItem } from '@arkturian/audio-guide';
import type { IView } from '../IView';

/**
 * YoutubePanelView — renders #nowPlaying + #songQueue and binds the
 * #ytNextBtn button. The actual <iframe> is managed by YoutubePlayer
 * service which mounts on the #ytPlayer element via the YT iframe API.
 */
export class YoutubePanelView implements IView {
  private _nowPlaying: HTMLElement | null = null;
  private _songQueue: HTMLElement | null = null;
  private _nextBtn: HTMLButtonElement | null = null;
  private _bus: EventManager | null = null;
  private _nextHandler: EventListener | null = null;

  private _onSongsChanged = (payload: { current: SongItem | null; queue: SongItem[] }): void => {
    if (this._nowPlaying) {
      if (payload.current) {
        const label = payload.current.title || payload.current.query;
        this._nowPlaying.textContent = `♪ ${label}`;
      } else {
        this._nowPlaying.textContent = '♪ Warte auf Song-Empfehlung';
      }
    }
    if (this._songQueue) {
      if (payload.queue.length === 0) {
        this._songQueue.textContent = '';
      } else {
        this._songQueue.innerHTML = payload.queue
          .map((s, i) => `<div>${i + 1}. ${s.title || s.query}</div>`)
          .join('');
      }
    }
  };

  mount(root: HTMLElement): void {
    this._nowPlaying = root.querySelector<HTMLElement>('#nowPlaying');
    this._songQueue = root.querySelector<HTMLElement>('#songQueue');
    this._nextBtn = root.querySelector<HTMLButtonElement>('#ytNextBtn');
  }

  bindEvents(bus: EventManager): void {
    this._bus = bus;
    bus.on('songs:changed', this._onSongsChanged);
    if (this._nextBtn) {
      this._nextHandler = () => bus.emit('user:yt-next');
      this._nextBtn.addEventListener('click', this._nextHandler);
    }
  }

  dispose(): void {
    if (this._bus) {
      this._bus.off('songs:changed', this._onSongsChanged);
      this._bus = null;
    }
    if (this._nextBtn && this._nextHandler) {
      this._nextBtn.removeEventListener('click', this._nextHandler);
      this._nextHandler = null;
    }
  }
}
