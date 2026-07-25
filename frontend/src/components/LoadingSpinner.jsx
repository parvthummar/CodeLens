import React from 'react';
import './LoadingSpinner.css';

const LoadingSpinner = ({ size = 40, message }) => {
  return (
    <div className="spinner-container">
      <div 
        className="spinner" 
        style={{ width: size, height: size }}
      />
      {message && <p className="spinner-message">{message}</p>}
    </div>
  );
};

export default LoadingSpinner;
