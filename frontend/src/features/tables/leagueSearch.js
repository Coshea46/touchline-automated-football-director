const normalized = (value) => String(value || '').normalize('NFKD').replace(/[\u0300-\u036f]/g, '')
  .toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
const queryText = (value) => normalized(value).split(' ').filter((word) =>
  !['where', 'is', 'are', 'show', 'me', 'the', 's', 'table', 'standings', 'league', 'in', 'which', 'find'].includes(word)).join(' ');

function distance(a, b) {
  const row = Array.from({ length: b.length + 1 }, (_, index) => index);
  for (let i = 1; i <= a.length; i += 1) {
    let diagonal = row[0];
    row[0] = i;
    for (let j = 1; j <= b.length; j += 1) {
      const previous = row[j];
      row[j] = Math.min(row[j] + 1, row[j - 1] + 1, diagonal + Number(a[i - 1] !== b[j - 1]));
      diagonal = previous;
    }
  }
  return row[b.length];
}

function similarity(query, alias) {
  const text = normalized(alias);
  if (!text || !query) return 0;
  if (text === query || queryText(text) === query) return 1;
  if (text.startsWith(query) || query.startsWith(text)) return .92;
  if (text.includes(query)) return .85;
  return 1 - distance(query, text) / Math.max(query.length, text.length);
}

export function searchCatalogue(catalogue, query) {
  const text = queryText(query);
  if (!text) return [];
  const competitions = Array.isArray(catalogue?.competitions) ? catalogue.competitions : [];
  const teams = Array.isArray(catalogue?.teams) ? catalogue.teams
    : competitions.flatMap((competition) => (competition.teams || []).map((team) => ({ ...team, competition_code: competition.code })));
  return [
    ...competitions.map((competition) => ({ kind: 'competition', competition_code: competition.code,
      name: competition.name, aliases: [competition.name, competition.code, ...(competition.aliases || [])] })),
    ...teams.map((team) => ({ kind: 'team', competition_code: team.competition_code, team_id: team.id,
      name: team.name, aliases: [team.name, team.short_name, ...(team.aliases || [])] })),
  ].map((item) => ({ ...item, confidence: Math.max(...item.aliases.map((alias) => similarity(text, alias))) }))
    .filter((item) => item.confidence >= .68).sort((a, b) => b.confidence - a.confidence || a.name.localeCompare(b.name)).slice(0, 8);
}
