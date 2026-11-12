import { CloudOff, ExternalLink } from 'lucide-react';
import { safeWebUrl } from '../api/platformApi';

export default function DataNotice({ data, error, kind, retry }) {
  const missing = data?.status === 'missing_key';
  const setup = safeWebUrl(data?.setup_url);
  const retryAfter = data?.retry_after_seconds ?? data?.retry_after;
  return <div className="data-notice" role="status"><CloudOff size={28} aria-hidden="true" />
    <h2>{missing ? `Connect ${kind} data` : data?.status === 'rate_limited' ? 'Updates temporarily paused' : `${kind === 'standings' ? 'Standings' : 'News'} unavailable`}</h2>
    <p>{error || data?.message || (data?.status === 'rate_limited' ? 'Too many update requests. Please wait and try again.'
      : 'The provider has not supplied data for this view. Please try again later.')}</p>
    {missing && <p>The site operator needs to connect the provider account.</p>}
    {Number.isFinite(retryAfter) && retryAfter > 0 && <p>Try again in about {retryAfter} seconds.</p>}
    <div className="data-actions">{setup && <a className="text-link" href={setup} target="_blank" rel="noreferrer">Provider setup <ExternalLink size={14} aria-hidden="true" /></a>}
      <button className="primary-button" type="button" onClick={retry}>Try again</button></div>
  </div>;
}
