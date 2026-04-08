import type { EventManager } from '@arkturian/audio-guide';
import type { LocationSnapshot, CardinalDirection } from '@arkturian/audio-guide';
import type { TrackingSnapshot } from '@arkturian/audio-guide';
import type { IView } from '../IView';

const CARDINAL: CardinalDirection[] = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];

function bearingToDirection(deg: number | null): string {
  if (deg === null) return '?';
  return CARDINAL[Math.round(deg / 45) % 8];
}

/**
 * StatusPanelView — writes to #pos, #street, #compass, #arrow, #speed,
 * #status, #count. Purely reactive: subscribes to location:changed
 * and tracking:changed events and renders. Zero business logic.
 */
export class StatusPanelView implements IView {
  private _pos: HTMLElement | null = null;
  private _street: HTMLElement | null = null;
  private _compass: HTMLElement | null = null;
  private _arrow: HTMLElement | null = null;
  private _speed: HTMLElement | null = null;
  private _status: HTMLElement | null = null;
  private _count: HTMLElement | null = null;
  private _bus: EventManager | null = null;

  private _onLocation = (snap: LocationSnapshot): void => {
    if (this._pos && snap.lat !== null && snap.lon !== null) {
      this._pos.textContent = `${snap.lat.toFixed(5)}, ${snap.lon.toFixed(5)}`;
    }
    if (this._street && snap.street !== null) {
      this._street.textContent = snap.street;
    }
    if (this._compass) {
      const h = snap.effectiveHeading;
      this._compass.textContent = h !== null ? `${bearingToDirection(h)} (${Math.round(h)}°)` : '-';
    }
    if (this._arrow) {
      const h = snap.effectiveHeading;
      this._arrow.style.transform = h !== null ? `rotate(${h}deg)` : '';
    }
    if (this._speed) {
      if (snap.speed !== null) {
        const kmh = Math.round(snap.speed * 3.6);
        this._speed.textContent = `${kmh} km/h`;
      } else {
        this._speed.textContent = '-';
      }
    }
  };

  private _onTracking = (snap: TrackingSnapshot): void => {
    if (this._count) {
      this._count.textContent = `${snap.sentCount} updates sent`;
    }
  };

  private _onStatus = (payload: { text: string }): void => {
    if (this._status) {
      this._status.textContent = payload.text;
    }
  };

  mount(root: HTMLElement): void {
    this._pos = root.querySelector<HTMLElement>('#pos');
    this._street = root.querySelector<HTMLElement>('#street');
    this._compass = root.querySelector<HTMLElement>('#compass');
    this._arrow = root.querySelector<HTMLElement>('#arrow');
    this._speed = root.querySelector<HTMLElement>('#speed');
    this._status = root.querySelector<HTMLElement>('#status');
    this._count = root.querySelector<HTMLElement>('#count');
  }

  bindEvents(bus: EventManager): void {
    this._bus = bus;
    bus.on('location:changed', this._onLocation);
    bus.on('tracking:changed', this._onTracking);
    bus.on('status:changed', this._onStatus);
  }

  dispose(): void {
    if (this._bus) {
      this._bus.off('location:changed', this._onLocation);
      this._bus.off('tracking:changed', this._onTracking);
      this._bus.off('status:changed', this._onStatus);
      this._bus = null;
    }
  }
}
