function goToDownloadpage() {
  window.location.href = "download.html";
}

function displayGithub() {
  window.location.href =
    "https://github.com/JohnnieM-CS426/GameTranslationTool";
}

function displayDownloadPage() {
  let download = document.querySelector(".download-page");

  if (download.classList.contains("show")) {
    download.classList.remove("show");
  } else {
    download.classList.add("show");
  }

  document.body.innerHTML = "";
  document.body.appendChild(download);
}

let downloadButton2 = document.querySelector(".firstButton");
downloadButton2.addEventListener("click", goToDownloadpage);

let githubButton = document.querySelector(".mac-button, .windows-button");
githubButton.addEventListener("click", displayGithub);

let downloadButton = document.querySelector(".firstButton");
downloadButton.addEventListener("click", displayDownloadPage);
