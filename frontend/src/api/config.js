// Set VITE_API_URL at build time (Vercel env var); falls back to the local API.
export const API_BASE = import.meta.env.VITE_API_URL ?? 'http://127.0.0.1:8000';
