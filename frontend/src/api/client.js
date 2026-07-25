const API_BASE = 'http://127.0.0.1:8000';

async function request(endpoint, options = {}) {
  const token = localStorage.getItem('token');
  const headers = {
    'Content-Type': 'application/json',
    ...options.headers,
  };
  if (token) headers['Authorization'] = `Bearer ${token}`;

  const response = await fetch(`${API_BASE}${endpoint}`, {
    ...options,
    headers,
  });

  if (response.status === 401) {
    localStorage.removeItem('token');
    window.location.href = '/login';
    return;
  }

  if (response.status === 204) return null;
  
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: 'An error occurred' }));
    throw new Error(error.detail || 'An error occurred');
  }

  return response.json();
}

export const api = {
  signup: (data) => request('/api/v1/auth/signup', { method: 'POST', body: JSON.stringify(data) }),
  login: (data) => request('/api/v1/auth/login', { method: 'POST', body: JSON.stringify(data) }),
  getProjects: () => request('/api/v1/projects/'),
  getProject: (id) => request(`/api/v1/projects/${id}`),
  createProject: (data) => request('/api/v1/projects/', { method: 'POST', body: JSON.stringify(data) }),
  deleteProject: (id) => request(`/api/v1/projects/${id}`, { method: 'DELETE' }),
  searchProject: (id, data) => request(`/api/v1/projects/${id}/search`, { method: 'POST', body: JSON.stringify(data) }),
};
