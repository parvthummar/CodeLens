import React, { createContext, useState, useContext } from 'react';
import { api } from '../api/client';
import { useNavigate } from 'react-router-dom';

export const AuthContext = createContext();

export const useAuth = () => useContext(AuthContext);

const getStoredUser = () => {
  const email = localStorage.getItem('user_email');
  return email ? { email } : null;
};

export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState(getStoredUser);
  const [token, setToken] = useState(localStorage.getItem('token'));
  const navigate = useNavigate();

  const isAuthenticated = !!token;

  const login = async (email, password) => {
    const data = await api.login({ email, password });
    localStorage.setItem('token', data.access_token);
    localStorage.setItem('user_email', email);
    setToken(data.access_token);
    setUser({ email });
  };

  const signup = async (email, password, full_name) => {
    await api.signup({ email, password, full_name });
  };

  const logout = () => {
    localStorage.removeItem('token');
    localStorage.removeItem('user_email');
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
