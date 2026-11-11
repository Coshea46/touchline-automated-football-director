const fs = require('node:fs');
const path = require('node:path');

// CRA imports data from src. The backend package owns the only maintained copy.
const repositoryRoot = path.resolve(__dirname, '..');
const source = path.join(repositoryRoot, 'backend', 'src', 'touchline', 'data', 'matches.json');
const destination = path.join(repositoryRoot, 'frontend', 'src', 'data', 'matches.json');

try {
  // Read first: a missing canonical catalogue must fail the calling npm command.
  const catalogue = fs.readFileSync(source);
  const current = fs.existsSync(destination) ? fs.readFileSync(destination) : null;
  if (!current || !current.equals(catalogue)) {
    fs.mkdirSync(path.dirname(destination), { recursive: true });
    fs.writeFileSync(destination, catalogue);
  }
  console.log('Fixture catalogue synced from backend package data.');
} catch (error) {
  console.error(`Cannot sync fixture catalogue: ${error.message}`);
  process.exitCode = 1;
}
