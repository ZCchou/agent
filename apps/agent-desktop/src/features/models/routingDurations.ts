export function intentTimeout(value: string): number | null {
  const seconds = Number(value);
  return value.trim() && Number.isInteger(seconds) && seconds >= 30 && seconds <= 180 ? seconds : null;
}
export function idleMinutes(value: string): number | null {
  const minutes = Number(value);
  return value.trim() && Number.isInteger(minutes) && minutes >= 0 && minutes <= 1440 ? minutes * 60 : null;
}
