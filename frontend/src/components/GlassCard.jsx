import React from 'react';
import './GlassCard.css';

const GlassCard = ({ children, className = '', onClick }) => {
  return (
    <div className={`glass-card-component glass-card ${className}`} onClick={onClick}>
      {children}
    </div>
  );
};

export default GlassCard;
