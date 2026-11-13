import { useEffect, useRef, useState } from 'react';
import './StartupExperience.css';

export default function StartupExperience({ ready, onEnter, onCancel, cancelled, onRetry, onManual, notice }) {
  const [phase, setPhase] = useState('tutorial');
  const video = useRef(null);
  const heading = useRef(null);
  useEffect(() => { heading.current?.focus(); }, [phase, cancelled]);
  useEffect(() => {
    const timer = setTimeout(onCancel, 120000);
    return () => clearTimeout(timer);
  }, [onCancel]);
  useEffect(() => {
    if (phase !== 'setup' || !ready || cancelled) return undefined;
    const timer = setTimeout(onEnter, 350);
    return () => clearTimeout(timer);
  }, [phase, ready, cancelled, onEnter]);
  return <section className={`startup-screen ${phase === 'setup' && ready && !cancelled ? 'startup-leaving' : ''}`}
    role="dialog" aria-modal="true" aria-labelledby="startup-title">
    <div className="startup-card">
      <p className="startup-brand">TOUCHLINE <span>YOUR MATCHES. YOUR DIRECTOR.</span></p>
      <h1 id="startup-title" tabIndex={-1} ref={heading}>{cancelled ? 'Setup paused' : phase === 'tutorial' ? 'Welcome to Touchline' : 'Setting things up'}</h1>
      {cancelled ? <><p>{notice || 'Setup stopped. Your match clock has stayed paused. Start again when you’re ready.'}</p>
        <button onClick={onRetry}>Try again</button>
        {onManual && <button className="startup-cancel" onClick={onManual}>Continue without AI</button>}</> : <>
        {phase === 'tutorial' ? <>
          <video ref={video} className="startup-video" src="/media/touchline-tutorial.mp4" autoPlay muted controls playsInline
            aria-label="Touchline tutorial" onEnded={() => setPhase('setup')} onError={() => setPhase('setup')} />
          <p>A quick introduction while your Director gets ready. Use the video controls to turn narration on.</p>
          <button onClick={() => { video.current?.pause(); setPhase('setup'); }}>Skip tutorial</button>
        </> : <>
          <div className="startup-orbit" aria-hidden="true"><span /><span /><span /></div>
          <p role="status" aria-live="polite">{ready ? 'Your Director is ready. Let’s play.' : 'Preparing your private replay and starting the AI Director…'}</p>
          <p className="startup-detail">Your matches are paused at 00:00. First startup can take a little longer.</p>
        </>}
        <button className="startup-cancel" onClick={onCancel}>Cancel setup</button>
      </>}
    </div>
  </section>;
}
