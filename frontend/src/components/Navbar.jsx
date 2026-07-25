import React from 'react';
import { useAuth } from '../context/AuthContext';
import './Navbar.css';

const Navbar = () => {
  const { user, logout } = useAuth();

  return (
    <nav className="navbar">
      <div className="navbar-container">
        <div className="navbar-brand">CodeSearch</div>
        <div className="navbar-actions">
          {user && <span className="navbar-email">{user.email}</span>}
          <button className="btn-ghost" onClick={logout}>Logout</button>
        </div>
      </div>
    </nav>
  );
};

export default Navbar;
