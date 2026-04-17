document.addEventListener("DOMContentLoaded", function(){

function displayGithub() {
  window.location.href =
    "https://github.com/1001-Galela-Amiel/JamX-Translator";
}

let macButton = document.querySelector(".mac-button");
let windowsButton = document.querySelector(".windows-button");
let githubButton = document.querySelector(".github-button");

if(macButton) {
  macButton.addEventListener("click", displayGithub);
}

if(windowsButton) {
  windowsButton.addEventListener("click", displayGithub);
}

if(githubButton) {
  githubButton.addEventListener("click", displayGithub);
}

});

function toggleMenu() {
  const menuContent = document.querySelector(".menu-content");
  menuContent.classList.toggle("active");
}

document.addEventListener("click", function (event) {
  const menu = document.querySelector(".menu-content");
  const button = document.querySelector(".menu-btn");

  if (!menu.contains(event.target) && !button.contains(event.target)) {
    menu.classList.remove("active");
  }
});