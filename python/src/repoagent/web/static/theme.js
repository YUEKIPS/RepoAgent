"use strict";
// Synchronous same-origin bootstrap: apply saved colors before the stylesheet paints.
globalThis.RepoTheme=(()=>{
  const ids=['ink','blue','slate','forest','sand'],key='repoagent-theme';
  function read(){try{return localStorage.getItem(key);}catch{return null;}}
  function apply(value){
    const theme=ids.includes(value)?value:'ink';
    document.documentElement.setAttribute('data-theme',theme);
    try{localStorage.setItem(key,theme);}catch{/* A blocked preference store does not stop the app. */}
    return theme;
  }
  apply(read());
  return {ids,apply,current:()=>document.documentElement.getAttribute('data-theme')};
})();
