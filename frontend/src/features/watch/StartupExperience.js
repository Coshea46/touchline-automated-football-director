import { useEffect, useRef, useState } from 'react';
import './StartupExperience.css';

export default function StartupExperience({ ready, onEnter, onCancel, cancelled, onRetry, onManual, notice,
  deferFailure = false, retryAllowed = true }) {
  const [phase, setPhase] = useState('tutorial');
  const [muted, setMuted] = useState(false);
  const [playBlocked, setPlayBlocked] = useState(false);
  const video = useRef(null);
  const heading = useRef(null);
  const showFailure = cancelled && (!deferFailure || phase !== 'tutorial');
  useEffect(() => {
    const player = video.current;
    if (phase !== 'tutorial' || showFailure || !player) return undefined;
    let active = true;
    const play = async () => {
      try {
        await player.play();
      } catch {
        if (!active) return;
        player.muted = true;
        setMuted(true);
        try {
          await player.play();
        } catch {
          if (active) setPlayBlocked(true);
        }
      }
    };
    play();
    return () => { active = false; };
  }, [phase, showFailure]);
  const enableSound = () => {
    const player = video.current;
    if (!player) return;
    // Keep this call synchronous with the click so browser user activation applies.
    player.muted = false;
    if (player.volume === 0) player.volume = 1;
    setMuted(false);
    player.play()?.then(() => setPlayBlocked(false)).catch(() => setPlayBlocked(true));
  };
  useEffect(() => { heading.current?.focus(); }, [phase, showFailure]);
  useEffect(() => {
    if (cancelled) return undefined;
    const timer = setTimeout(() => onCancel('AI startup took too long. Setup has stopped and your matches have not started. You can try again or continue without AI.', true), 120000);
    return () => clearTimeout(timer);
  }, [onCancel, cancelled]);
  useEffect(() => {
    if (phase !== 'setup' || !ready || cancelled) return undefined;
    const timer = setTimeout(onEnter, 350);
    return () => clearTimeout(timer);
  }, [phase, ready, cancelled, onEnter]);
  return <section className={`startup-screen ${phase === 'setup' && ready && !cancelled ? 'startup-leaving' : ''}`}
    role="dialog" aria-modal="true" aria-labelledby="startup-title">
    <div className="startup-card">
      <p className="startup-brand">TOUCHLINE <span>YOUR MATCHES. YOUR DIRECTOR.</span></p>
      <h1 id="startup-title" tabIndex={-1} ref={heading}>{showFailure ? 'Setup paused' : phase === 'tutorial' ? 'Welcome to Touchline' : 'Setting things up'}</h1>
      {showFailure ? <><p>{notice || 'Setup stopped. Your match clock has stayed paused. Start again when you’re ready.'}</p>
        {retryAllowed && <button onClick={onRetry}>Try again</button>}
        {onManual && <button className="startup-cancel" onClick={onManual}>Continue without AI</button>}</> : <>
        {phase === 'tutorial' ? <>
          <video ref={video} className="startup-video" src="/media/touchline-tutorial.mp4?v=3" autoPlay muted={muted} controls playsInline
            aria-label="Touchline tutorial" onVolumeChange={(event) => setMuted(event.currentTarget.muted || event.currentTarget.volume === 0)}
            onPlay={() => setPlayBlocked(false)} onEnded={() => setPhase('setup')} onError={() => setPhase('setup')} />
          {(muted || playBlocked) && <button className="startup-sound" onClick={enableSound}>{playBlocked ? 'Play tutorial with sound' : 'Turn sound on'}</button>}
          <p>{cancelled ? 'You can finish the tutorial, then continue without AI.' : 'A quick introduction while your Director gets ready.'}</p>
          <p className="startup-detail">The matches shown here are recordings used to demonstrate Touchline. They are not live.</p>
          {cancelled && notice && <p role="status" aria-live="polite" className="startup-detail">{notice}</p>}
          <button onClick={() => { video.current?.pause(); setPhase('setup'); }}>Skip tutorial</button>
        </> : <>
          <div className="startup-orbit" aria-hidden="true"><span /><span /><span /></div>
          <p role="status" aria-live="polite">{ready ? 'Your Director is ready. Let’s play.' : 'Preparing your private replay and starting the AI Director…'}</p>
          <p className="startup-detail">Getting your Director ready can take a little longer on your first visit.</p>
        </>}
        {!cancelled && <button className="startup-cancel" onClick={onCancel}>Cancel setup</button>}
      </>}
    </div>
  </section>;
}
