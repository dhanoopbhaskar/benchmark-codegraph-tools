export function useTheme() {
  return 'dark';
}

export const useAfter = (callback: () => void) => {
  return () => callback();
};
