using System.Collections;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.UI;


public class UIMenu_Mute : MonoBehaviour
{
    public bool isMuted;
    public GameObject item;
    // Start is called before the first frame update
    void Start()
    {
        
    }

    // Update is called once per frame
    void Update()
    {
        
    }

    public void SwitchMuted(){
        isMuted=!isMuted;
        ColorBlock colors = GetComponent<Button>().colors;        
        if (isMuted){            
            colors.normalColor=Color.red;
            colors.selectedColor=Color.red;
        }
        else{
            colors.normalColor=Color.gray;
            colors.selectedColor=Color.gray;
        }
        GetComponent<Button>().colors=colors;
        
        try{
            item.GetComponent<CustomAudioSource>().muted=isMuted;
        }
        catch{
            item.GetComponent<Array>().muted=isMuted;
        }
    }
}
