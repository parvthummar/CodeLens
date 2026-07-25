import React, { createContext, useState, useEffect } from 'react';
import { api } from '../api/client';
import { useNavigate } from 'react-router-dom';

export const AuthContext = createContext();

export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState(null);
  const [token, setToken] = useState(localStorage.getItem('token'));
  const [isAuthenticated, setIsAuthenticated] = useState(!!localStorage.getItem('token'));
  const navigate = useNavigate();

  useEffect(() => {
    setIsAuthenticated(!!token);
  }, [token]);

  const login = async (email, password) => {
    const data = await api.login({ email, password });
    localStorage.setItem('token', data.access_token);
    setToken(data.access_token);
    setUser({ email }); // simplistic user decoding
  };

  const signup = async (email, password, full_name) => {
    await api.signup({ email, password, full_name });
  };

  const logout = () => {
    localStorage.removeItem('token');
    setToken(null);
    setUser(null);
    navigate('/login');
  };

  return (
    <AuthContext.Provider value={{ user, token, isAuthenticated, login, signup, logout }}>
      {children}
    </AuthContext.Provider>
  );
};
