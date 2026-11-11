// jest-dom adds custom jest matchers for asserting on DOM nodes.
// allows you to do things like:
// expect(element).toHaveTextContent(/react/i)
// learn more: https://github.com/testing-library/jest-dom
import '@testing-library/jest-dom';

// jsdom doesn't implement media playback
window.HTMLMediaElement.prototype.play = () => Promise.resolve();
window.HTMLMediaElement.prototype.pause = () => {};

// Fake WebSocket so tests never touch the network and can push backend messages.
class FakeWebSocket {
  static instances = [];
  constructor(url) { this.url = url; this.readyState = 0; this.sent = []; FakeWebSocket.instances.push(this); }
  send(data) { if (this.readyState !== 1) throw new Error('Socket is closed'); this.sent.push(JSON.parse(data)); }
  close() { this.readyState = 3; }
  open() { this.readyState = 1; this.onopen?.(); }
  receive(message) { this.onmessage?.({ data: typeof message === 'string' ? message : JSON.stringify(message) }); }
  drop() { this.readyState = 3; this.onclose?.(); }
}
window.WebSocket = FakeWebSocket;
global.FakeWebSocket = FakeWebSocket;
beforeEach(() => { FakeWebSocket.instances = []; });
