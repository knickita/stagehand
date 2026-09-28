using System.Collections;
using System.Collections.Generic;
using TMPro;
using UnityEngine;
using UnityEngine.UI;

public class HierarchyItem : MonoBehaviour
{
    public GameObject item;
    public Button selectButton, muteButton, soloButton;
    public TMP_Text attenuation;
    // Start is called before the first frame update
    void Start()
    {
        
    }

    // Update is called once per frame
    void Update()
    {
        ColorBlock colors = selectButton.colors;        
        if (item.GetComponent<Selectable>().IsSelected()){            
            colors.normalColor=Color.blue;
            colors.selectedColor=Color.blue;
        }
        else{
            colors.normalColor=new Color(0,0,0,0);
            colors.selectedColor=new Color(0,0,0,0);
        }
        selectButton.colors=colors;
        //keep name updated
        selectButton.GetComponentInChildren<TMPro.TMP_Text>().text=item.name;
    }
}
