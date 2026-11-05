import { Button, IconButton } from './components/Button';
import { LegacyPanel } from './components/Legacy';
import { Plain } from './components/Plain';
import { useTheme } from './hooks/useTheme';
// import { Nope } from './components/Nope';

export default function App() {
  const theme = useTheme();
  return (
    <div className={theme}>
      <Button label="Save" onClick={() => save()} />
      <IconButton label="Cancel" />
      <LegacyPanel />
      <Plain />
    </div>
  );
}
