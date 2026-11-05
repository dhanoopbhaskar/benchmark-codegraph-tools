import { useTheme } from '../hooks/useTheme';

export interface ButtonProps {
  label: string;
  onClick: () => void;
}

export function Button({ label, onClick }: ButtonProps) {
  const theme = useTheme();
  return (
    <button className={theme} onClick={onClick}>
      {label}
    </button>
  );
}

export const IconButton = ({ label }: ButtonProps) => {
  return <Button label={label} onClick={() => {}} />;
};
