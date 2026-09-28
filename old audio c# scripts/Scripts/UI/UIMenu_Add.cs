using System.Collections;
using System.Collections.Generic;
using UnityEngine;

public class UIMenu_Add : MonoBehaviour
{
    public GameObject contentMain;    
    public GameObject contentPointSource;
    public GameObject contentArray;

 

    // Update is called once per frame
    void Update()
    {
        
    }

    public void CloseMenu(){
        ClosePointSource();
        CloseArray();
        contentMain.SetActive(false);
    }
    public void ClosePointSource(){
        contentPointSource.SetActive(false);
    }
    public void CloseArray(){
        contentArray.SetActive(false);
    }

    public void Interact(){
        if (contentMain.activeSelf){
            contentMain.SetActive(false);
        } else {
            contentMain.SetActive(true);
        }
    }

    public void InteractPointSource(){
        if (contentPointSource.activeSelf){
            contentPointSource.SetActive(false);
        } else {
            contentPointSource.SetActive(true);
        }
    }

    public void InteractArray(){
        if (contentArray.activeSelf){
            contentArray.SetActive(false);
        } else {
            contentArray.SetActive(true);
        }
    }
}
