import React from 'react';
import ReactDOM from 'react-dom/client';
import PlatformApp from './PlatformApp';
import { Theme } from './ui';
import './theme.css';
import './platform.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Theme accentColor="green" grayColor="sage" radius="medium" appearance="light">
      <PlatformApp />
    </Theme>
  </React.StrictMode>,
);
