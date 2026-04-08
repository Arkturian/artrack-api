/// <reference types="vite/client" />

// Vite's `?raw` suffix imports a file's content as a string
declare module '*?raw' {
  const content: string;
  export default content;
}

// Importing a .css file for its side-effect (bundling)
declare module '*.css';
