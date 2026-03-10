function goToDownloadpage() {
  window.location.href = "download.html";
}
let downloadButton = document.querySelector(".firstButton");

if(downloadButton) {
  downloadButton.addEventListener("click", goToDownloadpage);
}


function displayGithub() {
  window.location.href =
    "https://github.com/JohnnieM-CS426/GameTranslationTool";
}

let macButton = document.querySelector(".mac-button");
let windowsButton = document.querySelector(".windows-button");

if(macButton) {
  macButton.addEventListener("click", displayGithub);
}

if(windowsButton) {
  windowsButton.addEventListener("click", displayGithub);
}

