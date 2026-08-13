export function bootstrapTokenFromHash(hash: string): string | null {
  const fragment = hash.startsWith("#") ? hash.slice(1) : hash;
  const params = new URLSearchParams(fragment);
  const token = params.get("token");
  return token && token.trim() ? token : null;
}
